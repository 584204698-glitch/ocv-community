"""Durable, owner-scoped stage handoff for explicitly enabled dynamic jobs.

The stage implementations keep their own input, receipt and revision checks.
This coordinator dispatches once, never automatically retries a failed request,
and does not depend on an open browser or a stored login cookie.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2] / 'workspace' / 'one_click'
router = APIRouter(prefix='/api/one-click')
_executing = ContextVar('one_click_stage_execution', default=False)
_stop = threading.Event()
_thread = None
log = logging.getLogger(__name__)


def _path(user_id, job_id):
    if not re.fullmatch(r'[a-f0-9]{12,32}', str(job_id)):
        raise HTTPException(404, '制作任务不存在')
    return ROOT / str(int(user_id)) / (job_id + '.json')


def load(user_id, job_id):
    path = _path(user_id, job_id)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('user_id') != int(user_id) or data.get('job_id') != job_id:
        raise ValueError('一键制作归属记录不一致')
    return data


def save(state):
    path = _path(state['user_id'], state['job_id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    state['updated_at'] = time.time()
    temporary = path.with_name('.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def register(job):
    """Called only after the authenticated creator explicitly chose one-click."""
    state = dict(version=1, user_id=int(job.user_id), job_id=job.id,
                 project_id='', state='running', message='等待配音和字幕完成', claim=None)
    save(state)
    return state


def register_native(owner, record):
    """A native story owns its run directly; there is no TTS pipeline job."""
    state = dict(version=2, kind='native_story', user_id=int(owner), job_id=record['id'],
                 project_id=record['id'], state='running', message='等待角色与对话分镜规划', claim=None)
    save(state)
    return state


def assert_manual_edit(record):
    if _executing.get() or record.get('creation_parameters', {}).get('dynamic_execution_owner') != 'server':
        return
    # Explicit pausing is required before changing the inputs of an automatic run.
    if record.get('native_story'):
        state = load(record['native_owner_id'], record['id'])
    else:
        from .pipeline import store
        source = (record.get('source_project') or {}).get('id')
        job = store.get(source) if source else None
        state = load(job.user_id, source) if job else None
    if state and state['state'] == 'running':
        raise HTTPException(409, '一键制作正在后台继续，请先点击“暂停后编辑”；当前阶段会保留。')


def next_action(record):
    """Return a safe stage transition or a visible pause reason."""
    status = record.get('status')
    shots = record.get('shots') or []
    if record.get('archived'):
        return None, '项目已移入回收站'
    if record.get('error'):
        return None, str(record['error'])
    if record.get('reedit_shot_id'):
        return None, '项目正在局部编辑，请完成检查后继续'
    for shot in shots:
        task = shot.get('image_task') or {}
        if task.get('status') == 'running':
            return None, ''
        if task.get('action') == 'auto_repair' and task.get('status') == 'failed':
            return None, f"{shot['id']} 核心图辅助修复失败，请核对原请求后再处理；未自动重发。"
    if status == 'completed':
        return 'complete', ''
    if status in {'planning', 'image_generating', 'video_generating', 'exporting'}:
        return None, ''
    if status in {'stopping', 'image_stopping', 'video_stopping', 'export_failed', 'failed'}:
        return None, '当前阶段已停止或失败，请检查后手动继续'
    if status == 'draft' and not shots:
        return 'plan', ''
    if status == 'storyboard_review' and shots:
        return 'review', ''
    if status == 'generation_ready':
        if any(s.get('image_status') == 'failed' or s.get('image_attempt_output') for s in shots):
            return None, '已有图片尝试尚未确认，请检查原请求后重试'
        return 'images', ''
    if status == 'image_review':
        if not shots:
            return None, '部分核心图未完成，请检查失败项后继续'
        pending = [s for s in shots if s.get('image_status') != 'completed']
        if pending:
            # A normal stop may leave never-submitted frames behind completed
            # ones. Continue only those; uncertain/failed attempts still pause.
            if any(s.get('image_status') not in {None, '', 'pending'} or any(s.get(key) for key in (
                    'image', 'image_error', 'image_attempt_output', 'image_task', 'image_history',
                    'native_image_repairs')) for s in pending):
                return None, '部分核心图未完成，请检查失败项后继续'
            return 'images', ''
        return 'confirm_images', ''
    if status in {'video_generation_ready', 'video_review'}:
        if not shots:
            return None, '项目没有可制作的镜头，请检查分镜'
        if any(s.get('dialogue_stale') and s.get('video_status') == 'completed' for s in shots):
            return None, '角色音色已修改，请局部重做受影响镜头后继续；旧请求和片段仍保留'
        pending = [s for s in shots if s.get('kind') == 'video' and s.get('video_status') != 'completed']
        for shot in pending:
            # A recovered test clip must not strand the untouched remainder.
            # Only never-submitted pending shots may advance automatically;
            # receipts, attempts and uncertain states still need explicit review.
            if (shot.get('video_status') not in {None, '', 'pending'}
                    or any(shot.get(key) for key in (
                        'video_error', 'video_task_id', 'video_request',
                        'video_state_file', 'video_execution_started', 'video_resume_available'))
                    or (shot.get('video_attempt') and shot.get('video_not_submitted') is not True)):
                detail = str(shot.get('video_error') or '').strip()
                return None, (f"{shot.get('id', '当前镜头')} 的动态请求失败或待核实。"
                              + (detail[:900] + '。' if detail else '')
                              + '请在“动态镜头”选中该镜并点击“继续处理本镜”；修复后可继续一键制作')
        return ('videos', '') if pending else ('export', '')
    return None, '当前阶段需要人工检查，自动制作已暂停'


def _pause(state, message, project_revision=None):
    revision = project_revision if project_revision is not None else (state.get('claim') or {}).get('revision')
    state.update(state='paused', message=str(message)[:1200], claim=None,
                 paused_project_revision=revision)
    save(state)


def _preserve_produced_shots(project, reports):
    """An automatic image review cannot undo a completed/accepted video.

    A user can still reopen that shot through the ordinary local-edit route.
    Review findings remain visible; preserving media does not approve quality.
    """
    affected = {issue.get('shot_id') for report in reports
                for issue in (report.get('review') or {}).get('issues', [])}
    protected = [shot['id'] for shot in project.get('shots', [])
                 if shot['id'] in affected and (
                     shot.get('video') or shot.get('video_status') == 'completed'
                     or (not shot.get('video_terminal') and any(shot.get(key) for key in (
                         'video_task_id', 'video_request', 'video_execution_started'))))]
    if protected:
        raise ValueError('、'.join(protected) + ' 已有视频或已提交的视频请求，自动修图不会回退这些镜头。'
                         '本次检查未通过，问题保留；请在对应镜头选择“重新编辑本镜”后局部处理。')


def dispatch(action, user, project):
    from . import video_studio as studio, video_generation, video_export
    from .video_review_policy import strict_quality_review
    strict_review = strict_quality_review(project)
    identity, revision = project['id'], project['revision']
    if action in {'videos', 'export'}:
        pending=[s['id'] for s in project.get('shots',[]) if s.get('kind')=='video' and s.get('video_status')!='completed']
        scope=(pending[:1] if project.get('native_story') else pending) if action=='videos' else [None]
        for shot_id in scope:
            readiness_error = studio.storyboard_review_error(studio.directory(user['id'], identity), project,shot_id=shot_id)
            if readiness_error:
                raise ValueError(readiness_error)
    if action == 'plan':
        # Unlike other stage APIs, plan_for_user has no revision argument.
        # Hold the existing reentrant lock through its acceptance of the task.
        with studio.LOCK:
            current = studio.read(studio.directory(user['id'], identity))
            if _executing.get():
                job_id = identity if current.get('native_story') else (current.get('source_project') or {}).get('id')
                state = load(user['id'], job_id) if job_id else None
                if not state or state.get('state') != 'running':
                    return current
                if state.get('project_id') != identity or state.get('claim') != {'action':action,'revision':revision}:
                    raise ValueError('规划提交记录已变化，未启动新规划；请核对原任务')
            if current['revision'] != revision or next_action(current)[0] != 'plan':
                raise ValueError('项目版本或阶段已变化，未启动旧任务的规划；请重新读取项目')
            return studio.plan_for_user(identity, user)
    if action == 'review':
        return studio.review_for_user(identity, studio.Review(revision=revision), user)
    if action == 'images':
        return studio.generate_storyboard_images_for_user(identity, studio.Review(revision=revision), user)
    if action == 'confirm_images':
        if project.get('native_story') and strict_review:
            from .video_native_image_quality import check_native_core_images, CoreImageReviewRequired
            def paused():
                if not _executing.get():return False
                state=load(user['id'],identity)
                return not state or state['state'] != 'running'
            def progress(message):
                if not _executing.get():return
                with studio.LOCK:
                    state=load(user['id'],identity)
                    if state and state['state']=='running' and state.get('claim')=={'action':action,'revision':revision}:
                        state['message']=message;save(state)
            try:
                check_native_core_images(studio.directory(user['id'],identity),project,user['id'],cancelled=paused,progress=progress)
            except CoreImageReviewRequired as exc:
                if paused():return project
                if _executing.get():
                    # Keep eligibility and submission atomic with user pause.
                    # The repair worker durably saves its history before launch.
                    with studio.LOCK:
                        state = load(user['id'], identity)
                        current = studio.read(studio.directory(user['id'], identity))
                        if not state or state.get('state') != 'running':
                            return current
                        if (state.get('project_id') != identity
                                or state.get('claim') != {'action': action, 'revision': revision}
                                or current['revision'] != revision
                                or current.get('status') != 'image_review'):
                            raise ValueError('核心图检查期间任务或项目版本已变化，未提交修图；请重新检查')
                        _preserve_produced_shots(current, exc.reports)
                        issues = [issue for report in exc.reports
                                  for issue in (report.get('review') or {}).get('issues', [])]
                        by_id = {shot['id']: shot for shot in current.get('shots', [])}
                        if not issues or any(
                                issue.get('shot_id') not in by_id
                                or not isinstance(issue.get('correction'), str)
                                or not issue['correction'].strip() for issue in issues):
                            raise
                        affected_ids = {issue['shot_id'] for issue in issues}
                        affected = [by_id[shot_id] for shot_id in affected_ids]
                        if any(shot.get('native_image_repairs') for shot in affected):
                            raise
                        # Redraw also invalidates clips whose continuity source
                        # is this image; preserve those receipts and media too.
                        dependents = [shot for shot in by_id.values() if
                                      (shot.get('core_reference_used') or {}).get('source_shot_id') in affected_ids]
                        if any(shot.get('video_status') == 'completed' or any(shot.get(key) for key in (
                                'video', 'video_task_id', 'video_request', 'video_execution_started',
                                'video_state_file', 'video_attempt', 'video_history')) for shot in affected + dependents):
                            raise
                        from .video_native_image_repair import start_repair
                        return start_repair(identity, revision, user, exc.reports,
                                            allow_design_adaptation=False)
                _preserve_produced_shots(project, exc.reports)
                raise
            if paused():return project
        return studio.confirm_storyboard_images_for_user(identity, studio.StoryboardConfirmation(revision=revision), user)
    if action == 'videos':
        ids = [s['id'] for s in project['shots'] if s['kind'] == 'video' and s.get('video_status') != 'completed']
        if project.get('native_story') and strict_review:
            # Recheck the current pixels/contract when resuming after an update
            # or a local edit. Unchanged, already reviewed groups hit the cache.
            # Video-stage review never implicitly redraws an existing image.
            from .video_native_image_quality import check_native_core_images, CoreImageReviewRequired
            def images_paused():
                if not _executing.get():return False
                state=load(user['id'],identity)
                return not state or state['state'] != 'running'
            try:
                check_native_core_images(studio.directory(user['id'], identity), project, user['id'],
                                         cancelled=images_paused)
            except CoreImageReviewRequired as exc:
                if images_paused():return project
                _preserve_produced_shots(project, exc.reports)
                raise
            if images_paused():return project
            from .video_dialogue_quality import check_native_speech, NativeSpeechReviewRequired
            # Inspect each result before spending GPU time on the next shot.
            # Cached evidence is rechecked against the actual clip and plan.
            # This screens words only; it cannot certify voice, lips or identity.
            completed = [s for s in project['shots'] if s.get('video_status') == 'completed']
            if completed:
                speech_failure = None
                try:
                    check_native_speech(studio.directory(user['id'], identity), {**project, 'shots': completed})
                except NativeSpeechReviewRequired as exc:
                    speech_failure = exc
                if images_paused():return project
                # Compare rendered frames with this attempt's frozen core image
                # before spending GPU time on later shots. Export reuses the
                # content-bound review, rather than treating words as visual QA.
                from .video_quality import check_automatic_export
                try:
                    check_automatic_export(studio.directory(user['id'], identity),
                                           {**project, 'shots': completed}, user['id'])
                except ValueError as exc:
                    if speech_failure:
                        raise ValueError(f'{speech_failure} 同时，{exc}') from exc
                    raise
                if speech_failure:
                    raise speech_failure
            if _executing.get():
                state = load(user['id'], identity)
                if not state or state['state'] != 'running':
                    return project  # User paused while CPU recognition ran.
        if project.get('native_story'):
            ids = ids[:1]
        return video_generation.generate_for_user(identity, video_generation.GenerateClips(revision=revision, shot_ids=ids), user)
    if action == 'export':
        # The last clip has no following videos tick. Screen it here too so
        # a mismatch pauses before export, preserving the original clip.
        if project.get('native_story') and strict_review:
            from .video_dialogue_quality import check_native_speech
            check_native_speech(studio.directory(user['id'], identity), project)
            if _executing.get():
                state = load(user['id'], identity)
                if not state or state['state'] != 'running':return project
        return video_export.start_export_for_user(identity, video_export.ExportRequest(revision=revision, use_video_audio=True), user, review_generated=strict_review)
    raise ValueError('未知自动制作阶段')


def tick(user_id, job_id):
    from . import video_studio as studio
    from .pipeline import store
    from .db import get_user_by_id
    with studio.LOCK:
        state = load(user_id, job_id)
        if not state or state['state'] != 'running':
            return
        native = state.get('kind') == 'native_story'
        job = None if native else store.get(job_id)
        user = get_user_by_id(int(user_id))
        if not user or (not native and (not job or job.user_id != int(user_id))):
            _pause(state, '任务或所属账号不可用')
            return
        if state.get('claim'):
            _pause(state, '上次阶段提交未确认，已暂停以避免重复生成；请检查任务记录')
            return
        if not native and job.status in {'cancelled', 'failed'}:
            _pause(state, job.message or '配音任务已停止或失败')
            return
        if native and state.get('project_id') != job_id:
            _pause(state, '对话制作任务与项目不一致')
            return
        if not state.get('project_id'):
            if job.status in {'queued', 'running'}:
                return
            if job.request.get('_step_mode_stage') != 'audio_review':
                _pause(state, '配音阶段尚未达到可衔接状态')
                return
            # Import is idempotent by source job, unlike paid model stages.
            try:
                project = studio.import_project_for_user(studio.ImportProject(job_id=job.id, from_audio_task=True), user)
                state['project_id'] = project['id']
                save(state)
            except Exception as exc:
                _pause(state, getattr(exc, 'detail', str(exc)))
                return
        path = studio.directory(user_id, state['project_id'])
        try:
            project = studio.read(path)
            if native and (project.get('native_owner_id') != int(user_id) or not project.get('native_story')):
                raise ValueError('对话故事不属于当前制作任务')
            if project.get('audio'):
                from .subtitle_timing import bound_subtitle_times, wav_duration
                bound_subtitle_times(project['scenes'], wav_duration(path / project['audio']), repair_final_end=False)
        except Exception as exc:
            _pause(state, getattr(exc, 'detail', str(exc)))
            return
        action, reason = next_action(project)
        if native and action in {'review', 'images', 'confirm_images', 'videos', 'export'}:
            try:
                require_native_voices(user_id, project)
            except (ValueError, OSError) as exc:
                _pause(state, str(exc) if isinstance(exc, ValueError) else '角色参考音色不可用，请重新选择', project['revision'])
                return
        if reason:
            _pause(state, reason, project['revision'])
            return
        if action == 'complete':
            state.update(state='completed', message='成片已生成，可预览和下载', claim=None)
            save(state)
            return
        if not action or studio.ACTIVE or studio.project_has_image_edits(project['id']):
            return
        state.update(claim={'action': action, 'revision': project['revision']}, message='后台正在衔接：' + action)
        save(state)
        claim = copy.deepcopy(state['claim'])
    # CPU speech recognition may take seconds. Keep the durable claim, but
    # release the project lock so status, project switching and pause work.
    token = _executing.set(True)
    try:
        dispatch(action, user, project)
    except Exception as exc:
        with studio.LOCK:
            current = load(user_id, job_id)
            if current and current['state'] == 'running' and current.get('claim') == claim:
                _pause(current, getattr(exc, 'detail', str(exc)))
    else:
        with studio.LOCK:
            current = load(user_id, job_id)
            if current and current['state'] == 'running' and current.get('claim') == claim:
                current.update(claim=None, last_action=action)
                save(current)
    finally:
        _executing.reset(token)


def _loop():
    while not _stop.wait(2):
        for path in sorted(ROOT.glob('*/*.json')):
            if _stop.is_set():
                return
            try:
                tick(int(path.parent.name), path.stem)
            except Exception:
                log.exception('One-click coordinator could not inspect %s', path.name)


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='ocv-one-click', daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=3)


def _owned_job(request, job_id):
    from .auth import require_user
    from .pipeline import store
    user = require_user(request)
    state = load(user['id'], job_id)
    if state and state.get('kind') == 'native_story':
        from . import video_studio as studio
        record = studio.read(studio.directory(user['id'], job_id), include_archived=True)
        if (state.get('project_id') != job_id or record.get('native_owner_id') != int(user['id'])
                or not record.get('native_story')):
            raise HTTPException(404, '制作任务不存在')
        return user, None
    job = store.get(job_id)
    if not job or job.user_id != int(user['id']):
        raise HTTPException(404, '制作任务不存在')
    if not job.request.get('dynamic_video') or not job.request.get('dynamic_auto_advance'):
        raise HTTPException(409, '这个项目未选择一键制作')
    return user, job


@router.get('/{job_id}')
def status(job_id: str, request: Request):
    from . import video_studio as studio
    user, job = _owned_job(request, job_id)
    with studio.LOCK:
        state = load(user['id'], job_id)
        if state and state.get('project_id'):
            record_path = studio.directory(user['id'], state['project_id']) / 'record.json'
            project = json.loads(record_path.read_text(encoding='utf-8')) if record_path.is_file() else {}
            if not project or project.get('archived'):
                return {**state, 'state': 'paused', 'message': '关联视频项目不存在或已移入回收站，请先恢复项目'}
            if project.get('status') == 'completed':
                return {**state, 'state': 'completed', 'message': '成片已生成，可预览和下载'}
            if state['state'] == 'completed':
                # A finished run is not automatically resumed by a manual edit.
                # Report the current project truth without mutating on a GET or
                # claiming that the previous export includes the new changes.
                return {**state, 'state': 'paused',
                        'message': '项目已有局部修改；请等待当前处理结束，检查后继续一键制作以更新成片'}
            if state['state'] == 'paused':
                # Derive the banner from the same transition logic used by
                # resume. A stale pause message cannot describe new assets.
                _, current_reason = next_action(project)
                message = project.get('error') or (
                    '当前阶段正在处理，自动衔接仍已暂停；处理结束后可继续。'
                    if project.get('status') in {'planning','image_generating','video_generating','exporting'}
                    else state['message'] if state.get('paused_project_revision') == project.get('revision') and state.get('message')
                    else current_reason or '自动衔接已暂停；当前阶段可以继续制作。')
                return {**state, 'message': message, 'project_revision': project.get('revision'),
                        'project_status': project.get('status')}
        return state or {'state': 'unmanaged', 'job_id': job_id, 'message': '此任务尚未启用后台自动衔接'}


class Control(BaseModel):
    action: str


def _resume_after_active_stage(state, project, path):
    """Re-arm the coordinator, never re-dispatch an already running stage."""
    from . import video_studio as studio
    if str(path) not in studio.ACTIVE:
        return False
    _, reason = next_action(project)
    if (not state or reason or project.get('status') not in
            {'planning', 'image_generating', 'video_generating', 'exporting'}):
        raise HTTPException(409, reason or '请等待当前阶段结束后再继续')
    state.update(state='running', message='已恢复自动衔接；等待当前阶段完成，不会重复提交', claim=None)
    save(state)
    return True


@router.post('/{job_id}')
def control(job_id: str, data: Control, request: Request):
    from . import video_studio as studio
    from .pipeline import store
    user, job = _owned_job(request, job_id)
    if data.action not in {'pause', 'resume'}:
        raise HTTPException(400, '未知操作')
    with studio.LOCK:
        state = load(user['id'], job_id)
        if data.action == 'pause':
            if not state:
                raise HTTPException(409, '任务尚未启用后台制作')
            _pause(state, '已暂停自动衔接；已提交的当前阶段仍会完成，完成后可以编辑')
            return state
        if job is None:
            project = studio.read(studio.directory(user['id'], job_id))
            if project.get('status') == 'completed':
                raise HTTPException(409, '成片已完成，无需继续')
            if state['state'] == 'running':
                return state
            if _resume_after_active_stage(state, project, studio.directory(user['id'], job_id)):
                return state
            if (project.get('status') == 'draft' and not project.get('shots')
                    and project.get('error') and not project.get('reedit_shot_id')):
                # This authenticated resume click is an explicit retry. Use
                # the ordinary planner so saved cast images and uncertain
                # paid-request receipts keep their normal reuse protections.
                # A failed retry pauses again; the coordinator never loops it.
                studio.plan_for_user(job_id, user, retry_native=True)
                state.update(state='running', message='正在重试未完成的分镜规划；已完成的角色素材会保留',
                             claim=None, last_action='plan')
                save(state)
                return state
            from .video_dialogue import silent_external_engine_equivalent
            refreshed = False
            for shot in project.get('shots', []):
                if shot.get('dialogue_stale') and silent_external_engine_equivalent(
                        user['id'], studio.directory(user['id'], job_id), project, shot):
                    shot.pop('dialogue_stale', None)
                    refreshed = True
            action, reason = next_action(project)
            if reason:
                raise HTTPException(409, reason)
            if action in {'review', 'images', 'confirm_images', 'videos', 'export'}:
                try:
                    require_native_voices(user['id'], project)
                except (ValueError, OSError) as exc:
                    raise HTTPException(409, str(exc) if isinstance(exc, ValueError) else '角色参考音色不可用') from exc
            if refreshed:
                project['revision'] += 1
                studio.save(studio.directory(user['id'], job_id), project)
            state.update(state='running', message='已继续 H3 原生对话制作', claim=None)
            save(state)
            return state
        if job.status in {'failed', 'cancelled'}:
            raise HTTPException(409, '请先检查并恢复原配音任务')
        if state and state['state'] == 'running':
            return state
        project = studio.find_audio_storyboard(user['id'], job.id)
        if project:
            if project.get('status') == 'completed':
                raise HTTPException(409, '成片已完成，无需继续')
            if _resume_after_active_stage(state, project, studio.directory(user['id'], project['id'])):
                return state
            _, reason = next_action(project)
            if reason:
                raise HTTPException(409, reason)
            project.setdefault('creation_parameters', {})['dynamic_execution_owner'] = 'server'
            studio.save(studio.directory(user['id'], project['id']), project)
        if job.status in {'queued', 'running'} and not state:
            raise HTTPException(409, '旧任务请等配音完成后再启用后台衔接')
        store.update(job, request={**job.request, 'dynamic_execution_owner': 'server'})
        state = state or register(job)
        state.update(state='running', message='已继续后台制作', claim=None, project_id=project['id'] if project else '')
        save(state)
        return state


def require_native_voices(owner, project):
    from .video_dialogue import voice_view
    if not project.get('shots') or any(not s.get('native_dialogue') for s in project['shots']):
        raise ValueError('原生对话分镜尚未准备好，不能沿用旁白路线')
    needed = {t['character_id'] for s in project['shots'] for t in s['native_dialogue']['turns']}
    voices = {v['character_id']:v for v in voice_view(int(owner), project)['characters']}
    from .video_external_dialogue import enabled as exact_enabled
    allowed={'current'} if exact_enabled(project) else {'current','generated'}
    missing = [voices.get(i, {}).get('name', '未绑定角色') for i in sorted(needed)
               if voices.get(i, {}).get('status') not in allowed]
    if missing:
        raise ValueError('请在“角色音色”中为以下发言者设计或选择音色，然后继续一键制作：' + '、'.join(missing))
