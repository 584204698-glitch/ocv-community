"""User-triggered, resumable paid clip generation for reviewed storyboards.

No Agent calls or automatic generation on navigation. A clip's inputs and paid
task identity stay frozen until the provider confirms a terminal failure.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import video_studio as studio
from .video_model_config import load_config
from .comfyui_bridge import ComfyUIStopped, run_video_profile, video_profile, video_task_state, _connection
from .comfyui_guard import get_guard_prompt_id, validate_guard_connection
from .h3_prompt_agent import H3_SKILL_SOURCE, H3PromptValidationError, convert_for_h3, prepare_h3_video_prompt
from .video_inputs import resolve_video_inputs, validate_shot_inputs, effective_profile_id, asset_rows
from .video_character_coverage import character_coverage
from .video_director_contracts import enforce_no_auto_subtitles
from module6_dynamic_video import (
    DynamicVideoStopped, DynamicVideoTaskFailed, DynamicVideoTaskUnknown,
    FAILURE, RunningHubVideoProvider, VideoGenerationRequest, validate_request,
)


router = APIRouter(prefix='/api/video-studio')
VIDEO_STAGES = {'video_generation_ready', 'video_generating', 'video_stopping', 'video_review'}
VIDEO_GENERATION_ENTRY_STAGES = {'video_generation_ready', 'video_review', 'export_failed', 'completed'}
LOCAL_VIDEO_QUEUES: dict[str, list[str]] = {}
LOCAL_VIDEO_RUNNING: dict[str, str] = {}
LOCAL_VIDEO_COMPUTE_LOCK = threading.Lock()
NARRATION_PREVIEWS: set[tuple[str, str]] = set()


@contextmanager
def _local_compute_slot(cancelled, on_wait=lambda: None):
    """Serialize whole local runs, including TTS before H3 admission.

    The machine guard remains authoritative for other services/processes.
    Waiting here never submits a task or retries a rejected inference.
    """
    acquired = False
    notified = False
    try:
        while not cancelled.is_set():
            acquired = LOCAL_VIDEO_COMPUTE_LOCK.acquire(timeout=.25)
            if acquired:
                break
            if not notified:
                on_wait()
                notified = True
        yield acquired and not cancelled.is_set()
    finally:
        if acquired:
            LOCAL_VIDEO_COMPUTE_LOCK.release()


class GenerateClips(BaseModel):
    revision: int
    shot_ids: list[str] = Field(default_factory=list, max_length=1000)
    retry_failed: bool = False
    regenerate_completed: bool = False


class VideoHistoryAction(BaseModel):
    revision: int


class ManualDubTiming(VideoHistoryAction):
    basis: str = Field(pattern=r'^[a-f0-9]{64}$')
    starts: list[float] = Field(default_factory=list, max_length=100)
    reset: bool = False


class ManualH3Prompt(VideoHistoryAction):
    prompt: str = Field(default='', max_length=20000)
    reset: bool = False


@router.get('/{identity}/videos/{shot_id}/manual-h3-prompt')
def preview_manual_h3(identity: str, shot_id: str, request: Request):
    """Compile current bindings without adopting text or starting media work."""
    from .video_dialogue import prepare_dialogue
    with studio.LOCK:
        owner=studio.require_user(request)['id']
        path=studio.directory(owner,identity)
        record=studio.read(path)
        shot=studio._find_shot(record,shot_id)
        try:
            selection,images,_=validate_shot_inputs(owner,path,record,shot)
            compiled=prepare_dialogue(owner,path,record,shot,selection,images)
            if not compiled.get('external_dialogue'):
                raise ValueError('本镜当前绑定没有可复用的已准备配音，未生成新的配音')
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from exc
        return dict(project_id=identity,shot_id=shot_id,revision=record['revision'],prompt=compiled['prompt'])


@router.post('/{identity}/videos/{shot_id}/manual-h3-prompt')
def adopt_manual_h3(identity: str, shot_id: str, data: ManualH3Prompt, request: Request):
    from .video_dialogue import prepare_dialogue
    from .video_manual_h3 import validate
    with studio.LOCK:
        owner=studio.require_user(request)['id']
        path=studio.directory(owner,identity)
        record=studio.read(path)
        studio.editable(record,data.revision)
        if record['status'] not in VIDEO_GENERATION_ENTRY_STAGES:
            raise HTTPException(409,'请在动态镜头阶段、生成停止后采用提示词')
        shot=studio._find_shot(record,shot_id)
        if data.reset:
            shot.pop('manual_h3_prompt',None)
        else:
            try:
                selection,images,_=validate_shot_inputs(owner,path,record,shot)
                compiled=prepare_dialogue(owner,path,record,shot,selection,images)
                if not compiled.get('external_dialogue'):
                    raise ValueError('请先准备本镜配音；当前手动入口适用于已有配音的 H3 对白路线')
                prompt=validate(data.prompt,compiled)
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
            shot['manual_h3_prompt']=dict(prompt=prompt,fingerprint=compiled['fingerprint'])
        record['logs'].append(f'{shot_id}：'+('已恢复自动 H3 提示词' if data.reset else '已采用手动 H3 最终提示词，下次重新生成生效；未调用语言模型或生成服务。'))
        record['revision']+=1
        studio.save(path,record)
        return record


def _asset(path: Path, relative: str) -> Path:
    result = (path / relative).resolve()
    if path.resolve() not in result.parents:
        raise ValueError('视频资产路径无效')
    return result


def _h3_diagnostic_asset(path: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not re.fullmatch(
            r'assets/h3_diagnostics/[a-f0-9]{32}\.json', relative):
        raise ValueError('H3 诊断文件路径无效')
    try:
        output = _asset(path, relative)
    except (OSError, RuntimeError) as exc:
        raise ValueError('H3 诊断文件路径无效') from exc
    # A diagnostic never aliases another asset, even within the same project.
    if output != path.resolve() / relative:
        raise ValueError('H3 诊断文件路径无效')
    return output


def _save_h3_diagnostic(path: Path, shot: dict, exc: H3PromptValidationError) -> None:
    """Persist only the converter's bounded diagnostic, outside the shot record."""
    fields = ('version', 'source_fingerprint', 'input_mode', 'source', 'error',
              'raw_response', 'draft_prompt', 'raw_truncated', 'draft_truncated')
    diagnostic = {key: copy.deepcopy(exc.diagnostic[key]) for key in fields}
    content = json.dumps(diagnostic, ensure_ascii=False, allow_nan=False).encode('utf-8')
    relative = f'assets/h3_diagnostics/{uuid.uuid4().hex}.json'
    output = _h3_diagnostic_asset(path, relative)
    output.parent.mkdir(parents=True, exist_ok=True)
    output = _h3_diagnostic_asset(path, relative)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.h3-', suffix='.tmp',
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    shot['h3_prompt_diagnostic'] = {
        'file': relative, 'source_fingerprint': diagnostic['source_fingerprint'],
        'error': diagnostic['error'],
    }


def _load_h3_diagnostic(path: Path, shot: dict) -> dict | None:
    """Read a bounded, project-owned prior draft for full converter revalidation."""
    descriptor = shot.get('h3_prompt_diagnostic')
    if not isinstance(descriptor, dict):
        return None
    try:
        file = _h3_diagnostic_asset(path, descriptor.get('file'))
        with file.open('rb') as stream:
            content = stream.read(512 * 1024 + 1)
        if len(content) > 512 * 1024:
            return None
        diagnostic = json.loads(content)
    except (OSError, ValueError, RuntimeError):
        return None
    if (not isinstance(diagnostic, dict)
            or not isinstance(descriptor.get('source_fingerprint'), str)
            or diagnostic.get('source_fingerprint') != descriptor['source_fingerprint']):
        return None
    return diagnostic


def _state_path(path, shot):
    relative = shot.get('video_state_file')
    return _asset(path, relative) if isinstance(relative, str) and relative else None


def _state(path, shot):
    file = _state_path(path, shot)
    if file is None or not file.exists():
        return None
    try:
        value = json.loads(file.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('原视频任务状态文件无法读取；为避免重复扣费，暂不允许重新提交') from exc
    if not isinstance(value, dict):
        raise ValueError('原视频任务状态资料无效，禁止重新提交')
    return value


def _sync_identity(path, shot):
    state = _state(path, shot)
    if state is None:
        # Absence of a task file is not evidence that a paid POST never happened.
        # Keep the independent task identity and fail closed after execution began.
        shot.update(video_terminal=False, video_resume_available=False, video_not_submitted=False)
        if shot.get('video_task_id') or (shot.get('video_request') and
                shot.get('video_execution_started') is not False):
            raise ValueError('原视频任务状态文件缺失，已保留任务编号；为避免重复扣费，禁止重新提交，请保留项目并联系支持核实。')
        return state
    if not state:
        raise ValueError('原视频任务状态资料为空，禁止重新提交')
    task_id = str(state.get('task_id') or '')
    if shot.get('video_task_id') and task_id != shot['video_task_id']:
        raise ValueError('原视频任务编号与状态文件不一致，禁止重新提交，请保留项目并联系支持核实。')
    terminal = bool(state.get('terminal') and state.get('status') in FAILURE)
    safe_pause = state.get('status') == 'PAUSED_BEFORE_SUBMIT' and state.get('paid_submission_started') is False
    shot.update(video_task_id=task_id, video_terminal=terminal,
                video_resume_available=(bool(state.get('task_id')) or safe_pause) and not terminal,
                video_not_submitted=safe_pause)
    return state


def _has_guard_receipt(path, shot):
    return bool(shot.get('video') and
                (_asset(path, shot['video']).parent / 'comfyui_submission' / 'receipt.json').is_file())


def recover_interrupted(path, record):
    """Recovery only exposes saved state. It never queries or submits a task."""
    for shot in record.get('shots', []):
        if shot.get('kind') != 'video' or shot.get('video_status') != 'running':
            continue
        if shot.get('video_backend') == 'comfyui':
            resumable = bool(shot.get('video_task_id') or _has_guard_receipt(path, shot))
            shot.update(video_status='stopped' if resumable else 'failed',
                        video_error=('上次本地等待已中断，原提交记录已保留；继续处理会先核对原任务并取回结果。'
                                     if resumable else '上次本地 ComfyUI 处理已中断，尚无已保存的任务编号，请检查本镜提交记录。'),
                        video_resume_available=resumable, video_terminal=not resumable)
            continue
        try:
            state = _sync_identity(path, shot)
            shot['video_status'] = 'failed' if shot.get('video_terminal') else ('pending' if not state or shot.get('video_not_submitted') else 'unknown')
            shot['video_error'] = ('云端已确认失败，可检查后选择重新付费生成。' if shot.get('video_terminal') else
                '上次本地处理已中断；继续时只查询原任务，不重复提交。' if shot.get('video_task_id') else
                '提交结果尚未确认且未取得任务编号，请先向服务商核实，禁止再次付费提交。' if state and not shot.get('video_not_submitted') else
                '尚未提交，可继续生成。')
        except ValueError as exc:
            shot.update(video_status='unknown', video_error=str(exc), video_resume_available=False,
                        video_terminal=False)
    record.update(status='video_review', error='上次动态镜头处理已中断，已完成片段和原任务身份均已保留。',
                  revision=record['revision'] + 1)
    record.setdefault('logs', []).append(record['error'])


def recover_missing_clips(path, record):
    """Missing local results can be retrieved again without paying for a new clip."""
    changed = False
    for shot in record.get('shots', []):
        if shot.get('kind') != 'video' or shot.get('video_status') != 'completed':
            continue
        try:
            file = _asset(path, str(shot.get('video') or ''))
            if file.is_file() and file.stat().st_size:
                continue
            if shot.get('video_backend') == 'comfyui':
                resumable = bool(shot.get('video_task_id') or _has_guard_receipt(path, shot))
                shot.update(video_status='stopped' if resumable else 'failed',
                            video_error=('本地视频文件缺失，可继续查询原任务并取回结果。'
                                         if resumable else '本地视频文件缺失且没有已保存的任务编号，请检查本镜记录。'),
                            video_resume_available=resumable, video_terminal=not resumable)
                changed = True
                continue
            _sync_identity(path, shot)
        except (OSError, ValueError) as exc:
            shot.update(video_resume_available=False, video_terminal=False)
            message = str(exc)
        else:
            message = '本地视频文件缺失，可继续查询原任务并重新下载；不会重新提交付费生成。'
        shot.update(video_status='unknown', video_error=message)
        changed = True
    if changed:
        record['revision'] += 1
    return changed


def _inputs(path, record, shot):
    from .video_core_references import dependency_error
    problem = dependency_error(path, record, shot)
    if problem:
        raise ValueError(problem + '；请局部更新本镜核心图后再提交视频')
    if shot.get('image_status') != 'completed':
        raise ValueError('请先完成并确认本镜核心分镜图')
    if shot.get('design_needs_review'):
        raise ValueError('本镜字幕范围调整后尚未确认设计')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', str(shot.get('id') or '')):
        raise ValueError('镜头编号无效')
    # No global asset dump: exactly the numbers used by the video finalizer.
    core = studio._storyboard_image_path(path, shot)
    selected = [Path(value) for value in studio._shot_reference_paths(path, record, shot)]
    return (core, *selected)


def _freeze_shot_audio(path: Path, shot: dict, destination: Path) -> None:
    """Cut the confirmed TTS interval into the immutable local request."""
    source = path / 'assets' / 'audio.wav'
    if not source.is_file():
        raise ValueError('当前动态项目缺少完整配音，无法传入本镜参考音频')
    start = float(shot.get('start') or 0)
    duration = float(shot.get('duration') or 0)
    if start < 0 or not 0 < duration <= 15:
        raise ValueError('本镜配音时间范围无效，无法裁剪参考音频')
    project_root = Path(__file__).resolve().parents[2]
    local = project_root / 'tools' / 'ffmpeg' / 'bin' / 'ffmpeg.exe'
    ffmpeg = str(local) if local.is_file() else (shutil.which('ffmpeg') or 'ffmpeg')
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run([
        ffmpeg, '-y', '-i', str(source), '-ss', f'{start:.3f}', '-t', f'{duration:.3f}',
        '-vn', '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(destination),
    ], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    if completed.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 44:
        raise ValueError('裁剪本镜参考配音失败：' + (completed.stderr or 'FFmpeg 未生成有效 WAV')[-1200:])


def _request(path, shot):
    saved = shot.get('video_request')
    if not isinstance(saved, dict):
        raise ValueError('缺少原视频请求快照，禁止重新提交')
    return VideoGenerationRequest(
        prompt=saved['prompt'], image_paths=tuple(_asset(path, item) for item in saved['reference_files']),
        output_path=_asset(path, shot['video']), duration=saved['duration'], ratio=saved['ratio'],
        resolution=saved['resolution'], generate_audio=False, real_person_mode=False,
    )


def _freeze_request(path, record, shot, config):
    """Copy only selected references, before the worker can make a paid POST."""
    inputs = _inputs(path, record, shot)
    prompt = enforce_no_auto_subtitles(shot.get('video_prompt'))
    seconds = float(shot.get('duration') or 0)
    duration = shot.get('generation_duration')
    if not 0 < seconds <= 15 or type(duration) is not int or duration < seconds:
        raise ValueError('本镜视频时长与字幕跨度不一致，请检查分镜')
    attempt = int(shot.get('video_attempt') or 0) + 1
    shot.pop('narration_preview',None)
    shot.pop('narration_preview_error',None)
    relative = f"assets/videos/{shot['id']}/attempt_{attempt:03d}"
    target = _asset(path, relative)
    if target.exists():
        raise ValueError('发现已有同编号视频请求资产；为避免覆盖付费任务，请先核实原任务')
    request = VideoGenerationRequest(prompt, tuple(inputs), target / 'clip.mp4', duration,
        record['settings'].get('ratio') or '16:9', config['resolution'])
    validate_request(request)
    references = [f'{relative}/references/ref_{number:02d}.jpg' for number in range(1, len(inputs) + 1)]
    snapshot = dict(prompt=prompt, duration=duration, use_duration=seconds, ratio=request.ratio,
                    resolution=request.resolution, reference_files=references,
                    base_url=config['base_url'], submit_path=config['submit_path'],
                    query_path=config.get('query_path'), upload_path=config.get('upload_path'),
                    account_fingerprint=hashlib.sha256(config['api_key'].encode('utf-8')).hexdigest())
    target.parent.mkdir(parents=True, exist_ok=True)
    # Prepare atomically. A bad local image must not leave an attempt directory
    # that looks like a submitted paid task or blocks a corrected retry.
    with tempfile.TemporaryDirectory(prefix='.preparing-', dir=target.parent) as temporary:
        staging = Path(temporary)
        for number, original in enumerate(inputs, 1):
            file = staging / 'references' / f'ref_{number:02d}.jpg'
            file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, file)
            studio._normalize_rgb_image(file, strict=True)
        # This file intentionally contains no API key.
        (staging / 'request.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        staging.replace(target)
    # Invalidation already archives the attempt and clears its evidence while
    # retaining the counter. Keep real failures, but do not archive an empty row.
    if shot.get('video_attempt') and any(shot.get(key) for key in (
            'video', 'video_request', 'video_task_id', 'video_state_file')):
        shot.setdefault('video_history', []).append({key: copy.deepcopy(shot.get(key)) for key in (
            'video_attempt', 'video_task_id', 'video_state_file', 'video_request', 'video', 'video_error')})
    shot.update(video_attempt=attempt, video_request=snapshot, video=f'{relative}/clip.mp4',
                video_state_file=f'{relative}/task.json', video_task_id='', video_terminal=False,
                video_resume_available=False, video_status='pending', video_error='', video_version='',
                video_resolution=config['resolution'], video_execution_started=False)


LIPSYNC_REFERENCE_AUDIO_INSTRUCTION = (
    '【参考音频与对口型】本次上传的参考音频只用于驱动画面中实际说话人物的口型、面部表情、停顿和动作节奏，'
    '请让可见发言者与音频自然、准确同步。参考音频不是最终成片音轨，不要生成播放器、字幕、波形或音频可视化；'
    '不要额外添加人物对白、旁白、歌声或背景音乐。可以正常生成与当前场景相符的环境音效和动作音效。\n'
)
RHYTHM_REFERENCE_AUDIO_INSTRUCTION = (
    '【参考音频】本次上传的参考音频只用于动作节奏、停顿和情绪参考，不要求人物对口型。'
    '参考音频不是最终成片音轨；不要额外添加人物对白、旁白、歌声或背景音乐，可以生成自然的环境音效和动作音效。\n'
)


def _prompt_with_reference_audio(prompt: str, *, lipsync: bool) -> str:
    return (LIPSYNC_REFERENCE_AUDIO_INSTRUCTION if lipsync else RHYTHM_REFERENCE_AUDIO_INSTRUCTION) + prompt


def _h3_profile(profile):
    graph = profile.get('workflow') or {}
    graph = graph.get('prompt') if isinstance(graph.get('prompt'), dict) else graph
    return any(isinstance(node, dict) and node.get('class_type') in {
        'MiniMaxH3ImageToVideo', 'MiniMaxH3ReferenceToVideo',
    } for node in graph.values())


def _confirmed_character_inputs(owner, path, record, shot, selection, images):
    coverage = character_coverage(owner, path, record, shot, selection, asset_rows(record))
    context = {'status': coverage['status'], 'characters': []}
    if coverage['status'] not in {'covered', 'empty'}:
        return context
    context['source'] = 'user_confirmed_images'
    context['source_fingerprint'] = shot['video_character_review']['source_fingerprint']
    for character in coverage['characters']:
        labels = []
        for index, item in enumerate(images, 1):
            if ((index == 1 and character['presence'] == 'opening')
                    or (item['role'] == 'last_frame' and character['in_last_frame'])
                    or (item['role'] == 'identity_reference'
                        and item['id'] == character['reference_id'])):
                labels.append(f'<Picture {index}>')
        context['characters'].append({'id': character['id'], 'name': character['name'],
                                      'presence': character['presence'], 'picture_labels': labels})
        if character.get('project_character_id'):
            context['characters'][-1].update(project_character_id=character['project_character_id'],
                identity_sha256=character['identity_sha256'], constraints=character['constraints'])
    return context


def _freeze_local_request(path, record, shot, profile_id, *, prompt=None, prompt_format='generic',
                          include_reference_audio=False, output_node_id='', h3_effects_only=False,
                          character_inputs=None, native_dialogue=None):
    """Freeze one local request without cloud task identity or billing state."""
    selection, inputs = resolve_video_inputs(path, record, shot)
    source_prompt = str(shot.get('video_prompt') or '').strip()
    if native_dialogue is not None:
        if include_reference_audio or h3_effects_only or selection['input_mode'] != 'composition_reference':
            raise ValueError('原生对话不能混用旁白音轨、禁止人声策略或首尾帧输入')
        prompt = native_dialogue['prompt']
        prompt_format = 'h3_native_dialogue'
    else:
        prompt = prepare_h3_video_prompt(prompt if prompt is not None else source_prompt,
                                        effects_only=h3_effects_only)
    seconds = float(shot.get('duration') or 0)
    duration = shot.get('generation_duration')
    if native_dialogue and (native_dialogue.get('external_narration') or native_dialogue.get('external_dialogue')):
        duration=int(native_dialogue['render_plan']['duration'])
        seconds=float(duration)
    if not prompt:
        raise ValueError('本镜视频提示词为空')
    if not 0 < seconds <= 15 or type(duration) is not int or duration < seconds:
        raise ValueError('本镜视频时长与字幕跨度不一致，请检查分镜')
    attempt = int(shot.get('video_attempt') or 0) + 1
    relative = f"assets/videos/{shot['id']}/attempt_{attempt:03d}"
    target = _asset(path, relative)
    if target.exists():
        raise ValueError('发现已有同编号本地视频资产，请保留项目并核实后重试')
    shot.pop('narration_preview',None)
    shot.pop('narration_preview_error',None)
    snapshot = dict(backend='comfyui', profile_id=profile_id, prompt=prompt,
                    source_prompt=source_prompt, prompt_format=prompt_format,
                    duration=duration, use_duration=seconds,
                    ratio=record['settings'].get('ratio') or '16:9',
                    input_mode=selection['input_mode'], reference_files=[], input_manifest=[],
                    output_node_id=str(output_node_id or ''),
                    seed=uuid.uuid4().int % (2**63 - 1))
    from .video_render_options import snapshot as render_snapshot
    snapshot['render_options'] = render_snapshot(record.get('creation_parameters'))
    if prompt_format.startswith('h3_'):
        snapshot['h3_skill_source'] = H3_SKILL_SOURCE
    if h3_effects_only:
        # This is a submitted instruction, not proof that the model obeyed it.
        snapshot['audio_policy'] = {'mode': 'effects_only', 'music': 'N/A',
                                    'enforcement': 'prompt', 'version': 1}
    if character_inputs is not None:
        snapshot['character_inputs'] = copy.deepcopy(character_inputs)
    target.mkdir(parents=True, exist_ok=False)
    try:
        for index, item in enumerate(inputs):
            name = 'core.jpg' if index == 0 else f'input_{index + 1:02d}.jpg'
            reference = target / 'references' / name
            reference.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item['path'], reference)
            studio._normalize_rgb_image(reference, strict=True)
            relative_file = reference.relative_to(path.resolve()).as_posix()
            digest = hashlib.sha256(reference.read_bytes()).hexdigest()
            snapshot['input_manifest'].append({key: item[key] for key in ('id', 'name', 'role', 'label')}
                | {'file': relative_file, 'sha256': digest})
            if item['role'] == 'last_frame':
                snapshot['last_image_file'] = relative_file
            else:
                snapshot['reference_files'].append(relative_file)
        if include_reference_audio:
            snapshot['reference_audio_lipsync'] = bool(shot.get('reference_audio_lipsync', True))
            audio_relative = f'{relative}/references/voice.wav'
            _freeze_shot_audio(path, shot, _asset(path, audio_relative))
            snapshot['reference_audio_file'] = audio_relative
        if native_dialogue is not None:
            from .h3_dialogue import PROMPT_REVISION
            snapshot['prompt_compiler_revision'] = PROMPT_REVISION
            snapshot['audio_policy'] = {'mode': 'native_dialogue', 'music': 'N/A',
                                        'enforcement': 'reference_conditioning', 'version': 1}
            snapshot['dialogue'] = {key: copy.deepcopy(native_dialogue[key])
                                   for key in ('plan', 'basis', 'fingerprint', 'version')}
            if native_dialogue.get('external_dialogue'):
                snapshot['audio_policy']={'mode':'external_dialogue','enforcement':'audio_guide_and_post_dub','version':1}
                snapshot['external_dialogue']=copy.deepcopy(native_dialogue['external_dialogue'])
                snapshot['dialogue']['plan']=copy.deepcopy(native_dialogue['render_plan'])
            if native_dialogue.get('external_narration'):
                snapshot['dialogue']['plan']=copy.deepcopy(native_dialogue['render_plan'])
                snapshot['external_narration']=copy.deepcopy(native_dialogue['external_narration'])
            if shot.get('native_visual_grounding'):
                snapshot['native_visual_grounding'] = copy.deepcopy(shot['native_visual_grounding'])
            snapshot['voice_manifest'] = []
            for index, voice in enumerate(native_dialogue['voices'], 1):
                audio_relative = f'{relative}/references/voice_{index:02d}.wav'
                audio_target = _asset(path, audio_relative)
                from .h3_voice_reference import freeze_voice_reference
                excerpt = freeze_voice_reference(voice['path'], audio_target, voice['sha256'])
                snapshot['voice_manifest'].append({key: value for key, value in voice.items() if key != 'path'}
                                                  | excerpt | {'file': audio_relative})
        (target / 'request.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    if shot.get('video_attempt') and any(shot.get(key) for key in (
            'video', 'video_request', 'video_task_id', 'video_state_file')):
        shot.setdefault('video_history', []).append({key: copy.deepcopy(shot.get(key)) for key in (
            'video_attempt', 'video_backend', 'video_task_id', 'video_state_file',
            'video_request', 'video', 'video_error', 'manual_dub_timing', 'dialogue_alignment',
            'narration_preview', 'narration_preview_error')})
    shot.update(video_attempt=attempt, video_backend='comfyui', video_request=snapshot,
                video=f'{relative}/clip.mp4', video_state_file='', video_task_id='',
                video_terminal=False, video_resume_available=False, video_status='pending',
                video_error='', video_version='', video_execution_started=False)
    shot.pop('manual_dub_timing', None)


def _recover_local_prompt_id(record, shot, *, path=None, user_id=None):
    """Recover task identity written by builds that logged but did not persist it."""
    abandoned = {str(item).strip() for item in shot.get('video_abandoned_task_ids') or [] if str(item).strip()}
    if path is not None and user_id is not None and shot.get('video'):
        validate_guard_connection(_connection(user_id),
                                  _asset(path, shot['video']).parent / 'comfyui_submission')
    current = str(shot.get('video_task_id') or '').strip()
    # The external guard can receive the task while the web process is down.
    # Its response belongs to this exact attempt, unlike a historical log.
    if path is not None and shot.get('video') and (shot.get('video_request') or {}).get('input_manifest'):
        recovered = get_guard_prompt_id(_asset(path, shot['video']).parent / 'comfyui_submission',
                                       require_resolved=True)
        if recovered and current and current not in abandoned and recovered != current:
            raise ValueError('本镜保存的任务编号与共享队列提交记录不一致，请保留记录并核实，暂不重新提交。')
        if recovered and recovered not in abandoned:
            shot['video_task_id'] = recovered
            return recovered
    if current and current not in abandoned:
        return current
    # These snapshots are created by builds that persist task identity before
    # logging submission. A prior attempt's log must never supply a new
    # attempt's task identity if uploading/submitting that attempt failed.
    if (shot.get('video_request') or {}).get('input_manifest'):
        return ''
    abandoned.update(str(row.get('video_task_id') or '').strip()
                     for row in shot.get('video_history') or [])
    identity = re.escape(str(shot.get('id') or ''))
    pattern = re.compile(rf'{identity}：ComfyUI 已接收任务\s+([0-9a-f-]{{16,80}})', re.I)
    for line in reversed(record.get('logs') or []):
        match = pattern.search(str(line))
        if match and match.group(1) not in abandoned:
            shot['video_task_id'] = match.group(1)
            return match.group(1)
    return ''


def _enqueue_local_videos(path, identities):
    """Append shots to the live per-project worker without duplicating work."""
    worker_key = str(path)
    queue = LOCAL_VIDEO_QUEUES.setdefault(worker_key, [])
    running = LOCAL_VIDEO_RUNNING.get(worker_key)
    added = []
    for identity in identities:
        if identity == running or identity in queue:
            continue
        queue.append(identity)
        added.append(identity)
    return added


def _start_local_worker(path, record, identities, user_id, profile_id, use_h3_agent=False,
                        use_reference_audio=False):
    cancelled = threading.Event()
    worker_key = str(path)
    studio.ACTIVE.add(worker_key)
    studio.CANCEL_EVENTS[worker_key] = cancelled
    LOCAL_VIDEO_QUEUES[worker_key] = list(dict.fromkeys(identities))

    def worker():
        try:
            while True:
                with studio.LOCK:
                    queue = LOCAL_VIDEO_QUEUES.setdefault(worker_key, [])
                    if not queue:
                        break
                    identity = queue.pop(0)
                    LOCAL_VIDEO_RUNNING[worker_key] = identity
                if cancelled.is_set():
                    break
                status, error = 'completed', ''
                h3_diagnostic_error = None
                cancelled_terminal = False
                # Preparation may fail before a request is frozen. Reset per
                # shot so failure persistence cannot read an unbound snapshot
                # or reuse the preceding shot's preview/error metadata.
                snapshot = {}
                # Prompt conversion and request freezing happen inside the worker.
                # Therefore a slow/failed language call cannot block the HTTP
                # response and never starts a GPU job with a partial prompt.
                with studio.LOCK:
                    current = studio.read(path)
                    shot = studio._find_shot(current, identity)
                    resume_prompt_id = _recover_local_prompt_id(current, shot, path=path, user_id=user_id) if shot.get('video_request') else ''
                    shot.update(video_status='pending', video_error='', video_execution_started=False)
                    if resume_prompt_id:
                        current['logs'].append(f'{identity}：发现原 ComfyUI 任务 {resume_prompt_id}，将续查并下载，不会重复提交。')
                    elif use_h3_agent:
                        current['logs'].append(f'{identity}：正在按本镜图片与增强 Skill 准备并校验 H3 提示词。')
                    else:
                        current['logs'].append(f'{identity}：沿用通用视频提示词，正在冻结本地生成请求。')
                    current['revision'] += 1
                    studio.save(path, current)

                # A complete computer/ComfyUI restart clears its in-memory
                # queue. Validate a remembered id before waiting on it forever.
                # Unknown connectivity is deliberately conservative: never
                # submit a duplicate merely because the server is unreachable.
                if resume_prompt_id:
                    local_state = video_task_state(user_id, resume_prompt_id)
                    if local_state in {'missing', 'failed'}:
                        with studio.LOCK:
                            current = studio.read(path)
                            shot = studio._find_shot(current, identity)
                            abandoned = shot.setdefault('video_abandoned_task_ids', [])
                            if resume_prompt_id not in abandoned:
                                abandoned.append(resume_prompt_id)
                            shot['video_task_id'] = ''
                            reason = ('已不在 ComfyUI 队列或历史中，判定为服务重启后失效'
                                      if local_state == 'missing' else '已在 ComfyUI 中明确失败')
                            current['logs'].append(
                                f'{identity}：原任务 {resume_prompt_id} {reason}；按本次重跑操作创建新任务。')
                            current['revision'] += 1
                            studio.save(path, current)
                        resume_prompt_id = ''
                    elif local_state == 'unknown':
                        with studio.LOCK:
                            current = studio.read(path)
                            current['logs'].append(
                                f'{identity}：暂时无法确认原 ComfyUI 任务 {resume_prompt_id} 的状态；为避免重复占用显存，继续按原任务查询。')
                            studio.save(path, current)

                try:
                    h3_prompt = None
                    h3_source = ''
                    native_dialogue = None
                    shot_reference_audio = False
                    shot_lipsync = True
                    with studio.LOCK:
                        source_record = studio.read(path)
                        source_shot = copy.deepcopy(studio._find_shot(source_record, identity))
                    if not resume_prompt_id:
                        from .video_prompt_inputs import assert_prompt_inputs_current
                        if not source_shot.get('native_dialogue'):
                            assert_prompt_inputs_current(user_id, path, source_record, source_shot)
                        input_selection, input_assets, input_profile = validate_shot_inputs(user_id, path, source_record, source_shot)
                        shot_profile_id = effective_profile_id(source_record, source_shot)
                        source_shot['_confirmed_character_inputs'] = _confirmed_character_inputs(
                            user_id, path, source_record, source_shot, input_selection, input_assets)
                        if source_shot.get('native_dialogue'):
                            original_native_plan = copy.deepcopy(source_shot['native_dialogue'])
                            from .video_native_preparation import prepare_native
                            with studio.project_language_scope(user_id, studio.planning_parameters(source_record)):
                                native_dialogue, h3_source = prepare_native(user_id,path,source_record,source_shot,input_selection,input_assets,audio=True)
                            h3_prompt = native_dialogue['prompt']
                    h3_effects_only = bool(not resume_prompt_id and native_dialogue is None and (use_h3_agent or _h3_profile(input_profile)))
                    # Effects-only narration and native character dialogue are distinct routes.
                    shot_reference_audio = bool(use_reference_audio and native_dialogue is None and not h3_effects_only and
                                                source_shot.get('reference_audio_enabled', True))
                    shot_lipsync = bool(source_shot.get('reference_audio_lipsync', True))
                    effective_source_prompt = enforce_no_auto_subtitles(source_shot.get('video_prompt'))
                    if shot_reference_audio:
                        effective_source_prompt = _prompt_with_reference_audio(effective_source_prompt, lipsync=shot_lipsync)
                    if use_h3_agent and not resume_prompt_id and native_dialogue is None:
                        duration = int(source_shot.get('generation_duration') or 0)
                        source_shot['video_prompt'] = effective_source_prompt
                        with studio.project_language_scope(user_id, studio.planning_parameters(source_record)):
                            h3_prompt, h3_source = convert_for_h3(
                                source_shot, duration, reference_audio=shot_reference_audio,
                                lipsync=bool(shot_reference_audio and shot_lipsync),
                                image_path=input_assets[0]['path'], input_mode=input_selection['input_mode'],
                                last_image_path=next((item['path'] for item in input_assets if item['role'] == 'last_frame'), None),
                                reference_images=[item for item in input_assets if item['role'] == 'identity_reference'],
                                previous_diagnostic=_load_h3_diagnostic(path, source_shot))
                    if cancelled.is_set():
                        raise ComfyUIStopped('H3 提示词准备完成前已停止；尚未启动本地生成。')
                    with studio.LOCK:
                        current = studio.read(path)
                        shot = studio._find_shot(current, identity)
                        if native_dialogue is not None and not resume_prompt_id:
                            if shot.get('native_dialogue') != original_native_plan:
                                raise ValueError('生成准备期间本镜台词或动作已变化，未提交视频，请按当前分镜重试')
                            shot['native_dialogue'] = copy.deepcopy(source_shot['native_dialogue'])
                            if source_shot.get('native_visual_grounding'):
                                shot['native_visual_grounding'] = copy.deepcopy(source_shot['native_visual_grounding'])
                            shot.pop('native_grounding_force_recheck', None)
                        if resume_prompt_id:
                            shot.update(video_status='running', video_error='', video_execution_started=True,
                                        video_task_id=resume_prompt_id, video_terminal=False,
                                        video_resume_available=True)
                            snapshot = copy.deepcopy(shot)
                            current['logs'].append(f'{identity}：开始续查原本地任务，不创建新的生成尝试。')
                            current['revision'] += 1
                            studio.save(path, current)
                            shot = None
                        elif use_h3_agent or native_dialogue is not None:
                            shot.update(h3_prompt=h3_prompt, h3_prompt_source=h3_source,
                                        h3_prompt_skill=H3_SKILL_SOURCE)
                            if source_shot.get('_h3_diagnostic_reused'):
                                current['logs'].append(f'{identity}：输入未变，已重新校验并采用上次 H3 草稿，未再次调用转换模型。')
                            else:
                                current['logs'].append(f'{identity}：H3 专用提示词已校验并缓存；通用提示词保持不变。')
                        if not resume_prompt_id:
                            _freeze_local_request(
                                path, current, shot, shot_profile_id,
                                prompt=h3_prompt if use_h3_agent or native_dialogue is not None else effective_source_prompt,
                                prompt_format='h3_' + input_selection['input_mode'] if h3_effects_only else 'generic',
                                include_reference_audio=shot_reference_audio,
                                output_node_id=input_profile.get('mappings', {}).get('output_node_id', ''),
                                h3_effects_only=h3_effects_only,
                                character_inputs=source_shot.get('_confirmed_character_inputs'),
                                native_dialogue=native_dialogue,
                            )
                            shot.update(video_status='running', video_error='', video_execution_started=True)
                            current['logs'].append(f'{identity}：开始调用本地 ComfyUI，使用预设 {shot_profile_id}，本地任务串行执行。')
                            current['revision'] += 1
                            studio.save(path, current)
                            snapshot = copy.deepcopy(shot)
                except ComfyUIStopped as exc:
                    status, error = 'stopped', str(exc)
                    cancelled_terminal=getattr(exc,'terminal',False)
                except H3PromptValidationError as exc:
                    status, error = 'failed', f'H3 提示词转换或本地请求准备失败：{str(exc)[:1600]}'
                    h3_diagnostic_error = exc
                except Exception as exc:
                    status, error = 'failed', f'H3 提示词转换或本地请求准备失败：{str(exc)[:1600]}' if use_h3_agent else str(exc)[:1800]

                def progress(message):
                    with studio.LOCK:
                        latest = studio.read(path)
                        latest['logs'].append(f'{identity}：{str(message)[:1800]}')
                        studio.save(path, latest)

                try:
                    if status != 'completed':
                        raise ComfyUIStopped(error)
                    saved = snapshot['video_request']
                    reference = _asset(path, saved['reference_files'][0])
                    reference_audio = (_asset(path, saved['reference_audio_file'])
                                       if saved.get('reference_audio_file') else None)
                    voice_references = []
                    dialogue_audio=None
                    if saved.get('external_dialogue'):
                        from .video_external_dialogue import verified
                        dialogue_audio=verified(path,saved['external_dialogue']['guide'])
                    for voice in saved.get('voice_manifest', []):
                        audio_file = _asset(path, voice['file'])
                        if hashlib.sha256(audio_file.read_bytes()).hexdigest() != voice['sha256']:
                            raise ValueError('冻结的角色音色文件已变化，不能带着错误的声音继续提交')
                        voice_references.append(audio_file)
                    def submitted(prompt_id):
                        with studio.LOCK:
                            latest = studio.read(path)
                            live = studio._find_shot(latest, identity)
                            live.update(video_task_id=prompt_id, video_terminal=False,
                                        video_resume_available=True, video_execution_started=True,
                                        video_not_submitted=False)
                            latest['revision'] += 1
                            studio.save(path, latest)
                    run_video_profile(
                        user_id, saved['profile_id'], prompt=saved['prompt'], image_path=reference,
                        duration=float(saved['duration']), ratio=saved['ratio'],
                        output_path=_asset(path, snapshot['video']), seed=int(saved.get('seed', -1)),
                        progress=progress, should_stop=cancelled.is_set,
                        existing_prompt_id=resume_prompt_id, on_submitted=submitted,
                        audio_path=reference_audio,
                        input_mode=saved.get('input_mode', 'single_first'),
                        last_image_path=_asset(path, saved['last_image_file']) if saved.get('last_image_file') else None,
                        reference_image_paths=[_asset(path, item) for item in saved['reference_files'][1:]],
                        existing_output_node_id=saved.get('output_node_id', ''),
                        voice_reference_paths=voice_references,
                        **({'dialogue_audio_path':dialogue_audio} if dialogue_audio is not None else {}),
                        render_options=saved.get('render_options'),
                    )
                    if saved.get('external_dialogue'):
                        from .video_external_dialogue import preview
                        try:
                            snapshot['narration_preview']=preview(path,snapshot)
                            snapshot['narration_preview_error']=''
                        except Exception as exc:
                            snapshot['narration_preview_error']=str(exc)[:1800]
                            raise
                    if saved.get('external_narration'):
                        from .video_external_narration import preview
                        try:
                            snapshot['narration_preview']=preview(path,snapshot)
                            snapshot['narration_preview_error']=''
                        except Exception as exc:
                            snapshot['narration_preview_error']=str(exc)
                except ComfyUIStopped as exc:
                    if status == 'completed':
                        status, error = 'stopped', str(exc)
                    cancelled_terminal = getattr(exc,'terminal',False)
                except Exception as exc:
                    status, error = 'failed', str(exc)[:1800]
                with studio.LOCK:
                    latest = studio.read(path)
                    live = studio._find_shot(latest, identity)
                    background_wait = status == 'stopped' and _has_guard_receipt(path, live) and not cancelled_terminal
                    live.update(video_status=status, video_error=error,
                                video_resume_available=background_wait, video_terminal=not background_wait)
                    if status == 'failed':
                        from .video_opening_repair import capture
                        try:
                            capture(path, live)
                        except OSError:
                            latest['logs'].append(f'{identity}：开场修图建议未能保存，原错误已保留。')
                    if cancelled_terminal and live.get('video_task_id'):
                        abandoned=live.setdefault('video_abandoned_task_ids',[])
                        if live['video_task_id'] not in abandoned:abandoned.append(live['video_task_id'])
                    live.pop('native_grounding_force_recheck', None)
                    if snapshot.get('narration_preview_error') and (snapshot.get('video_request') or {}).get('external_dialogue'):
                        live['narration_preview_error']=snapshot['narration_preview_error']
                    if h3_diagnostic_error is not None:
                        try:
                            _save_h3_diagnostic(path, live, h3_diagnostic_error)
                        except Exception:
                            # Diagnostic I/O cannot replace the validation error or
                            # authorize freezing/submitting an invalid video request.
                            latest['logs'].append(f'{identity}：本次 H3 校验失败诊断未能保存；原校验错误已保留。')
                        else:
                            latest['logs'].append(f'{identity}：H3 校验失败诊断已保存，可由本项目所属用户下载。')
                    if status == 'completed':
                        live['video_version'] = _video_version(_asset(path, live['video']))
                        if snapshot['video_request'].get('external_narration') or snapshot['video_request'].get('external_dialogue'):
                            live['narration_preview']=snapshot.get('narration_preview')
                            live['narration_preview_error']=snapshot.get('narration_preview_error','')
                            if 'dialogue_alignment' in snapshot:live['dialogue_alignment']=snapshot['dialogue_alignment']
                        if native_dialogue is not None:
                            live.pop('dialogue_stale', None)
                        latest['logs'].append(f'{identity}：本地 ComfyUI 视频已生成并归档，可直接预览。')
                    else:
                        latest['logs'].append(f'{identity}：{error or "本地生成已停止"}')
                    latest['revision'] += 1
                    studio.save(path, latest)
                with studio.LOCK:
                    LOCAL_VIDEO_RUNNING.pop(worker_key, None)
                # A failed CUDA context may leave ComfyUI reachable while its
                # worker thread is dead. Never enqueue another shot after any
                # failed stage; keep completed clips and untouched pending shots.
                # An unresolved guard likewise needs reconciliation first.
                if status != 'completed':
                    with studio.LOCK:
                        latest = studio.read(path)
                        latest['logs'].append('当前镜头未完成，已暂停后续镜头；完成项与原任务编号已保留，请处理问题后明确继续。')
                        studio.save(path, latest)
                    break
        except Exception as exc:
            with studio.LOCK:
                current = studio.read(path)
                current['error'] = str(exc)[:1800]
                current['logs'].append('本地 ComfyUI 动态镜头处理暂停：' + current['error'])
                studio.save(path, current)
        finally:
            with studio.LOCK:
                try:
                    current = studio.read(path)
                    for shot in current['shots']:
                        if shot.get('video_status') == 'running':
                            resumable = bool(shot.get('video_task_id') or _has_guard_receipt(path, shot))
                            shot.update(video_status='stopped' if resumable else 'failed',
                                        video_error=('本地等待已中断，原任务记录仍保留，可继续核对并取回结果。'
                                                     if resumable else '本地处理已中断，尚无已保存的任务编号，请检查提交记录。'),
                                        video_resume_available=resumable, video_terminal=not resumable)
                    completed = sum(shot.get('video_status') == 'completed' for shot in current['shots'] if shot['kind'] == 'video')
                    total = sum(shot['kind'] == 'video' for shot in current['shots'])
                    current.update(status='video_review', revision=current['revision'] + 1)
                    current['logs'].append(f'本地动态片段已完成 {completed}/{total}。请逐镜预览；静态镜头保留核心图。')
                    studio.save(path, current)
                finally:
                    LOCAL_VIDEO_QUEUES.pop(worker_key, None)
                    LOCAL_VIDEO_RUNNING.pop(worker_key, None)
                    studio.ACTIVE.discard(worker_key)
                    studio.CANCEL_EVENTS.pop(worker_key, None)

    def waiting():
        with studio.LOCK:
            current = studio.read(path)
            current['logs'].append('等待其他项目的本地制作完成；配音与视频将串行处理，尚未重复提交计算任务。')
            current['revision'] += 1
            studio.save(path, current)

    def serial_worker():
        with _local_compute_slot(cancelled, waiting) as admitted:
            if not admitted:
                cancelled.set()
            # Also run the existing cancellation/finalization path when a
            # waiting project is stopped, without preparing any audio/video.
            worker()

    threading.Thread(target=serial_worker, daemon=True, name='ocv-comfyui-video').start()


def _safe_message(exc, config):
    text = str(exc)
    secret = str(config.get('api_key') or '')
    if secret:
        text = text.replace(secret, '[已隐藏密钥]')
    return text[:1800]


def _video_version(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()[:20]


VIDEO_VERSION_FIELDS = (
    'video_attempt', 'video_backend', 'video_task_id', 'video_state_file',
    'video_request', 'video', 'video_error', 'video_version', 'video_status',
    'narration_preview', 'narration_preview_error',
    'manual_dub_timing', 'dialogue_alignment',
)


def _history_video(path: Path, shot: dict, index: int) -> tuple[dict, Path]:
    history = shot.get('video_history') or []
    if not 0 <= index < len(history) or not isinstance(history[index], dict):
        raise HTTPException(404, '历史视频版本不存在')
    entry = history[index]
    try:
        output = _asset(path, str(entry.get('video') or ''))
    except ValueError as exc:
        raise HTTPException(404, '历史视频文件已不存在') from exc
    if not output.is_file() or output.stat().st_size <= 0:
        raise HTTPException(404, '历史视频文件已不存在')
    return entry, output


def _archive_current_video(path: Path, shot: dict, reason: str) -> None:
    relative = str(shot.get('video') or '')
    if not relative:
        return
    try:
        output = _asset(path, relative)
    except ValueError:
        return
    if not output.is_file() or output.stat().st_size <= 0:
        return
    entry = {key: copy.deepcopy(shot.get(key)) for key in VIDEO_VERSION_FIELDS}
    entry.update(invalidated_at=time.time(), invalidated_reason=reason)
    history = list(shot.get('video_history') or [])
    if not history or history[-1].get('video') != relative:
        history.append(entry)
    shot['video_history'] = history


def _invalidate_export_after_video_choice(record: dict, message: str) -> None:
    record.pop('export', None)
    record.update(status='video_review', error='')
    record.setdefault('logs', []).append(message)


def _account_slots(config):
    keys = list(config.get('api_keys') or ([config.get('api_key')] if config.get('api_key') else []))
    per_key = max(1, int(config.get('per_key_concurrency') or 1))
    slots = [key for key in keys for _ in range(per_key)]
    if config.get('concurrency_mode') == 'manual':
        slots = slots[:max(1, int(config.get('total_concurrency') or 1))]
    return slots


def _config_for_shot(config, shot, fallback=0):
    slots = _account_slots(config)
    if not slots:
        raise ValueError('请先在“接口与服务”保存视频 API Key')
    expected = str((shot.get('video_request') or {}).get('account_fingerprint') or '')
    if expected:
        key = next((item for item in dict.fromkeys(slots)
                    if hashlib.sha256(item.encode('utf-8')).hexdigest() == expected), None)
        if not key:
            raise ValueError('原视频任务使用的 API 账号已不在当前配置中，请恢复原 Key 后继续查询')
    else:
        key = slots[fallback % len(slots)]
    return {**config, 'api_key': key}


def _start_worker(path, record, identities, configs):
    cancelled = threading.Event()
    halted = threading.Event()
    studio.ACTIVE.add(str(path))
    studio.CANCEL_EVENTS[str(path)] = cancelled

    def process(identity, config):
        if cancelled.is_set() or halted.is_set():
            return False
        with studio.LOCK:
            current = studio.read(path)
            shot = studio._find_shot(current, identity)
            shot.update(video_status='running', video_error='')
            current['logs'].append(f'{identity}：开始处理动态镜头，使用 {shot["duration"]} 秒，请求 {shot["video_request"]["duration"]} 秒。')
            current['revision'] += 1
            studio.save(path, current)
            snapshot = copy.deepcopy(shot)
        def progress(message):
            with studio.LOCK:
                latest = studio.read(path)
                live = studio._find_shot(latest, identity)
                _sync_identity(path, live)
                latest['logs'].append(f'{identity}：{_safe_message(message, config)}')
                studio.save(path, latest)
        status, error = 'completed', ''
        try:
            saved = snapshot['video_request']
            if (config['base_url'].rstrip('/') != saved['base_url'].rstrip('/')
                    or config['submit_path'] != saved['submit_path']
                    or config.get('query_path') != saved.get('query_path', config.get('query_path'))
                    or config.get('upload_path') != saved.get('upload_path', config.get('upload_path'))):
                raise ValueError('原视频任务使用另一套接口配置，请恢复原配置后继续查询，不会重新付费提交')
            provider = RunningHubVideoProvider(config['api_key'], base_url=saved['base_url'],
                                               submit_path=saved['submit_path'],
                                               query_path=saved.get('query_path'),
                                               upload_path=saved.get('upload_path'))
            frozen_request = _request(path, snapshot)
            validate_request(frozen_request)
            if cancelled.is_set():
                raise DynamicVideoStopped('尚未开始提交，已停止本地处理。')
            with studio.LOCK:
                latest = studio.read(path)
                live = studio._find_shot(latest, identity)
                live['video_execution_started'] = True
                studio.save(path, latest)
            provider.run(frozen_request, _state_path(path, snapshot),
                         progress=progress, should_stop=cancelled.is_set)
        except DynamicVideoStopped as exc:
            status, error = 'stopped', _safe_message(exc, config)
        except DynamicVideoTaskFailed as exc:
            status, error = 'failed', _safe_message(exc, config)
        except Exception as exc:
            status, error = 'unknown', _safe_message(exc, config)
        with studio.LOCK:
            latest = studio.read(path)
            live = studio._find_shot(latest, identity)
            live.update(video_status=status, video_error=error)
            try:
                state = _sync_identity(path, live)
                if live.get('video_terminal'):
                    live['video_status'] = 'failed'
                elif status == 'unknown' and not state:
                    live['video_status'] = 'pending'
            except ValueError as exc:
                live.update(video_status='unknown', video_resume_available=False, video_terminal=False,
                            video_error=str(exc))
            if live['video_status'] == 'completed':
                output = _asset(path, live['video'])
                live['video_version'] = _video_version(output)
                live['video_resume_available'] = False
                latest['logs'].append(f'{identity}：视频已下载，可直接预览。')
            else:
                latest['logs'].append(f'{identity}：{live["video_error"] or "已停止，本镜头资产已保留"}')
            latest['revision'] += 1
            studio.save(path, latest)
        if status != 'completed':
            halted.set()
        return status == 'completed'

    def worker():
        try:
            maximum = max(1, min(len(identities), len(_account_slots(next(iter(configs.values()))))))
            with ThreadPoolExecutor(max_workers=maximum, thread_name_prefix='ocv-video') as pool:
                futures = [pool.submit(process, identity, configs[identity]) for identity in identities]
                for future in as_completed(futures):
                    future.result()
        except Exception as exc:
            with studio.LOCK:
                current = studio.read(path)
                current['error'] = _safe_message(exc, next(iter(configs.values())))
                current['logs'].append('动态镜头处理暂停：' + current['error'])
                studio.save(path, current)
        finally:
            with studio.LOCK:
                try:
                    current = studio.read(path)
                    for shot in current['shots']:
                        if shot.get('video_status') == 'running':
                            shot.update(video_status='unknown', video_error='本地处理中断，请继续查询原任务。')
                            try:
                                _sync_identity(path, shot)
                            except ValueError:
                                shot['video_resume_available'] = False
                    completed = sum(shot.get('video_status') == 'completed' for shot in current['shots'] if shot['kind'] == 'video')
                    total = sum(shot['kind'] == 'video' for shot in current['shots'])
                    current.update(status='video_review', revision=current['revision'] + 1)
                    current['logs'].append(f'动态片段已完成 {completed}/{total}。' +
                        ('已安全停止；云端已接收的任务不会因本地停止而撤销，请继续查询结果。' if cancelled.is_set() else
                         '请逐镜预览；静态画面保留原图，本阶段尚未合成整片。'))
                    studio.save(path, current)
                finally:
                    studio.ACTIVE.discard(str(path))
                    studio.CANCEL_EVENTS.pop(str(path), None)

    thread = threading.Thread(target=worker, daemon=True)
    try:
        thread.start()
    except Exception:
        studio.ACTIVE.discard(str(path))
        studio.CANCEL_EVENTS.pop(str(path), None)
        raise


@router.post('/{identity}/videos/generate')
def generate(identity: str, data: GenerateClips, request: Request):
    return generate_for_user(identity, data, studio.require_user(request))


def _reconcile_terminal_reroll(user_id, path, record, shot, prompt_id):
    """Archive an ended remote attempt before an explicit new generation."""
    state=video_task_state(user_id,prompt_id)
    if state not in {'completed','failed'}:
        detail=('原任务仍在运行或排队，请先停止或等待完成' if state in {'running','pending'}
                else '暂不能确认原任务已结束，请继续查询原任务')
        raise HTTPException(409,detail+'；未提交新任务。')
    from .comfyui_guard import assert_submission_released,GuardWaitingStopped
    output=_asset(path,shot['video'])
    try:
        assert_submission_released(output.parent/'comfyui_submission',prompt_id)
    except (ValueError,OSError,GuardWaitingStopped) as exc:
        raise HTTPException(409,str(exc)) from exc
    if state=='completed' and not output.is_file():
        saved=shot['video_request']
        # Existing task retrieval uses only the frozen request, no prompt agent,
        # TTS synthesis or GPU submission. Preserve the paid output in history.
        try:
            run_video_profile(user_id,str(saved.get('profile_id') or ''),
                prompt=str(saved.get('prompt') or ''),image_path=path,
                duration=float(saved.get('use_duration') or shot['generation_duration']),
                ratio=str(saved.get('ratio') or record['settings']['ratio']),output_path=output,
                existing_prompt_id=prompt_id,existing_output_node_id=str(saved.get('output_node_id') or ''))
        except Exception as exc:
            raise HTTPException(409,'原结果归档未完成，未重新生成：'+str(exc)[:1200]) from exc
    if state=='completed':
        shot.update(video_status='completed',video_terminal=True,video_resume_available=False,
                    video_version=_video_version(output),video_error='')
    else:
        shot.update(video_status='failed',video_terminal=True,video_resume_available=False)
    record['logs'].append(f'{shot["id"]}：已确认旧任务后台结束并释放守锁，原记录保留；按本次明确操作重新生成。')


def generate_for_user(identity: str, data: GenerateClips, user: dict):
    with studio.LOCK:
        path = studio.directory(user['id'], identity)
        record = studio.read(path)
        from .one_click import assert_manual_edit
        assert_manual_edit(record)
        backend = str(record.get('creation_parameters', {}).get('video_generation_backend') or 'api')
        local_queue_append = (backend == 'comfyui' and str(path) in studio.ACTIVE
                              and record.get('status') == 'video_generating')
        if local_queue_append:
            if int(record.get('revision') or 0) != data.revision:
                raise HTTPException(409, '项目刚刚发生变化，请刷新后重试')
            if studio.project_has_image_edits(record['id']):
                raise HTTPException(409, '核心分镜图正在重绘，请等待完成后再生成对应视频')
        else:
            studio.editable(record, data.revision)
            if record['status'] not in VIDEO_GENERATION_ENTRY_STAGES:
                raise HTTPException(409, '请先确认核心分镜图，再生成动态镜头')
        if (str(path) in studio.ACTIVE and not local_queue_append) or studio.project_has_image_edits(record['id']):
            raise HTTPException(409, '此任务正在处理，请稍后重试')
        catalog = {shot['id']: shot for shot in record['shots']}
        if len(set(data.shot_ids)) != len(data.shot_ids) or any(identity not in catalog for identity in data.shot_ids):
            raise HTTPException(400, '选择的镜头不存在或重复')
        if data.shot_ids and any(catalog[identity]['kind'] != 'video' for identity in data.shot_ids):
            raise HTTPException(400, '静态镜头保留图片，不需要提交视频生成')
        if data.regenerate_completed:
            if not data.shot_ids:
                raise HTTPException(400, '重新生成必须明确选择镜头')
            rerolls = [catalog[shot_id] for shot_id in data.shot_ids]
            if local_queue_append and any(shot['id'] == LOCAL_VIDEO_RUNNING.get(str(path)) for shot in rerolls):
                raise HTTPException(409, '当前镜头已经在生成中，不能重复加入队列')
            for shot in rerolls:
                if backend == 'comfyui' and _has_guard_receipt(path, shot):
                    try:
                        guarded_id = _recover_local_prompt_id(record, shot, path=path,
                                                             user_id=int(user['id']))
                    except (ValueError, OSError) as exc:
                        raise HTTPException(409, str(exc)) from exc
                    if guarded_id and shot.get('video_status') != 'completed':
                        _reconcile_terminal_reroll(int(user['id']),path,record,shot,guarded_id)
                # "Regenerate all" means a genuinely fresh pass regardless of
                # whether the previous attempt completed, failed, stopped, is
                # resumable, or was only frozen but never submitted.
                studio._invalidate_shot_video(
                    record, shot, '用户保留当前核心图与提示词，要求重新生成本镜动态片段')
        selected = [shot for shot in record['shots'] if shot['kind'] == 'video'
                    and (not data.shot_ids or shot['id'] in data.shot_ids)
                    and shot.get('video_status') != 'completed']
        if not selected:
            raise HTTPException(409, '没有需要生成的动态镜头；已完成视频不会重复提交')
        from .video_dialogue import native_mode
        if native_mode(record) and (backend != 'comfyui' or any(not shot.get('native_dialogue') for shot in selected)):
            raise HTTPException(409, '原生对话必须使用 H3 多参工作流和对应台词计划，不能转入旁白生成路线')
        if backend == 'comfyui':
            profile_id = str(record.get('creation_parameters', {}).get('comfyui_profile_id') or '')
            try:
                user_id = int(user['id'])
                profiles_by_shot = {}
                # Validate all shots before starting the background worker. The
                # worker converts/finalizes one prompt and freezes its inputs
                # immediately before that specific local GPU task.
                for shot in selected:
                    if shot.get('video_request') and _recover_local_prompt_id(record, shot, path=path, user_id=user_id):
                        # Retrieval uses frozen request identity. Current candidate
                        # availability is only a prerequisite for a new submission.
                        continue
                    from .video_prompt_inputs import assert_prompt_inputs_current
                    assert_prompt_inputs_current(user_id, path, record, shot)
                    selected_inputs, selected_assets, shot_profile = validate_shot_inputs(user_id, path, record, shot)
                    from .video_render_options import apply as apply_render_options
                    apply_render_options(shot_profile, record.get('creation_parameters'))
                    if shot.get('native_dialogue'):
                        from .video_dialogue import prepare_dialogue
                        prompt_node = shot_profile.get('mappings', {}).get('prompt', {}).get('node_id')
                        if shot_profile.get('workflow', {}).get(prompt_node, {}).get('class_type') != 'MiniMaxH3ReferenceToVideo':
                            raise ValueError('原生对话需要提示词映射到 H3 Ref2VA 音频参考节点')
                        prepare_dialogue(user_id, path, record, shot, selected_inputs, selected_assets)
                    profiles_by_shot[shot['id']] = shot_profile
                    if not str(shot.get('video_prompt') or '').strip():
                        raise ValueError(f'{shot["id"]} 的视频提示词为空')
                use_h3_agent = bool(record.get('creation_parameters', {}).get('comfyui_h3_prompt_agent'))
                use_reference_audio = bool(record.get('creation_parameters', {}).get('comfyui_reference_audio'))
                audio_shots = ([shot for shot in selected if shot['id'] in profiles_by_shot
                                and not _h3_profile(profiles_by_shot[shot['id']])
                                and shot.get('reference_audio_enabled', True)]
                               if not use_h3_agent else [])
                for audio_shot in audio_shots if use_reference_audio else []:
                    audio_binding = profiles_by_shot[audio_shot['id']].get('mappings', {}).get('audio')
                    if not (isinstance(audio_binding, dict) and audio_binding.get('node_id') and audio_binding.get('input_name')):
                        raise ValueError('当前 ComfyUI 预设没有映射参考音频节点，请先在工作台配置“参考音频”')
                if local_queue_append:
                    added = _enqueue_local_videos(path, [shot['id'] for shot in selected])
                    if not added:
                        raise HTTPException(409, '所选镜头已在生成或等待队列中，无需重复添加')
                    for shot_id in added:
                        queued = catalog[shot_id]
                        queued.update(video_status='pending', video_error='', video_terminal=False)
                    record['logs'].append(
                        f'已将 {len(added)} 个镜头追加到本地 ComfyUI 队列；当前镜头完成后将按点击顺序继续生成。')
                else:
                    # The entire accepted batch is waiting for this serial
                    # worker, including retries. Do not keep showing the old
                    # failure until each later shot reaches the GPU.
                    for queued in selected:
                        queued.update(video_status='pending', video_error='', video_terminal=False)
                    record['logs'].append(
                        f'用户确认处理 {len(selected)} 个动态镜头；使用各镜头已选择的本地 ComfyUI 预设，'
                        f'{"启用 H3 提示词转换 Agent，" if use_h3_agent else ""}'
                        f'{f"其中 {len(audio_shots)} 镜注入对应 TTS 参考音频，" if use_reference_audio and audio_shots else ""}为控制显存按镜头串行生成。'
                    )
                record.update(status='video_generating', error='', revision=record['revision'] + 1)
                studio.save(path, record)
                if not local_queue_append:
                    _start_local_worker(path, record, [shot['id'] for shot in selected], user_id, profile_id,
                                        use_h3_agent=use_h3_agent, use_reference_audio=use_reference_audio)
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from exc
            return record
        try:
            config = load_config()
            if not str(config.get('api_key') or '').strip():
                raise ValueError('请先保存视频 API 配置，或明确确认复用兼容的图像 API 密钥')
            # Validate every selected shot before scheduling any paid work.
            for shot in selected:
                state = _sync_identity(path, shot)
                if state is not None:
                    if shot.get('video_terminal'):
                        if not data.retry_failed:
                            raise ValueError('所选镜头有云端已确认失败的任务；请单独选择“重新付费生成”，不会自动重试')
                    elif not state.get('task_id') and not shot.get('video_not_submitted'):
                        raise ValueError('有提交结果未知且没有任务编号的镜头，请向服务商核实；禁止自动重新提交')
                    else:
                        validate_request(_request(path, shot))
                        continue
                elif shot.get('video_request'):
                    validate_request(_request(path, shot))
                    continue
                _inputs(path, record, shot)
                validate_request(VideoGenerationRequest(str(shot.get('video_prompt') or ''),
                    _inputs(path, record, shot), path / 'unused.mp4', shot.get('generation_duration'),
                    record['settings'].get('ratio') or '16:9', config['resolution']))
            for index, shot in enumerate(selected):
                if not shot.get('video_request') or shot.get('video_terminal'):
                    fresh_config = {**config, 'api_key': _account_slots(config)[index % len(_account_slots(config))]}
                    _freeze_request(path, record, shot, fresh_config)
                    # If preparing a later shot fails or the app closes, preserve
                    # each already-frozen (not yet paid) request for the next click.
                    record['revision'] += 1
                    studio.save(path, record)
            execution_configs = {shot['id']: _config_for_shot(config, shot, index)
                                 for index, shot in enumerate(selected)}
            record.update(status='video_generating', error='', revision=record['revision'] + 1)
            record['logs'].append(f'用户确认处理 {len(selected)} 个动态镜头；按接口设置并行处理，遇到失败即停止尚未启动的镜头，完成项不重复生成。')
            studio.save(path, record)
            _start_worker(path, record, [shot['id'] for shot in selected], execution_configs)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, _safe_message(exc, locals().get('config', {}))) from exc
        return record


@router.post('/{identity}/videos/stop')
def stop(identity: str, request: Request):
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        if record['status'] in {'video_generating', 'video_stopping'}:
            event = studio.CANCEL_EVENTS.get(str(path))
            if event is not None:
                event.set()
                record.update(status='video_stopping', revision=record['revision'] + 1)
                if record.get('creation_parameters', {}).get('video_generation_backend') == 'comfyui':
                    record['logs'].append('已请求停止本地 ComfyUI 当前任务，并且不再提交后续镜头。')
                else:
                    record['logs'].append('已请求停止本地处理，不再提交后续镜头；已提交的云端付费任务不能保证撤销。')
                studio.save(path, record)
                return record
        # A prior disconnect/old "stop waiting" may leave a receipt-owned task
        # running after the worker exited. Let the user cancel that same task.
        owner=studio.require_user(request)['id']
        snapshots=[copy.deepcopy(s) for s in record['shots']
                   if s.get('video_status')!='completed' and _has_guard_receipt(path,s)
                   and not s.get('video_terminal')]
    if snapshots:
        from .comfyui_cancel import cancel
        results=[]
        for shot in snapshots:
            try:
                report=cancel(_connection(owner),_asset(path,shot['video']).parent/'comfyui_submission')
            except Exception as exc:
                report=dict(terminal=False,message='本镜取消未完成：'+str(exc)[:1500])
            results.append((shot,report))
        with studio.LOCK:
            record=studio.read(path)
            for old,report in results:
                shot=studio._find_shot(record,old['id'])
                if shot.get('video')!=old.get('video'):continue
                terminal=report.get('status') in {'cancelled','not_submitted'}
                shot.update(video_status='stopped',video_error=report['message'],
                            video_resume_available=not terminal,video_terminal=terminal)
                if terminal and shot.get('video_task_id'):
                    abandoned=shot.setdefault('video_abandoned_task_ids',[])
                    if shot['video_task_id'] not in abandoned:abandoned.append(shot['video_task_id'])
                record['logs'].append(old['id']+'：'+report['message'])
            record['revision']+=1
            studio.save(path,record)
    return record


@router.get('/{identity}/shots/{shot_id}/h3-diagnostic')
def h3_diagnostic(identity: str, shot_id: str, request: Request):
    with studio.LOCK:
        user_id = studio.require_user(request)['id']
        path = studio.directory(user_id, identity)
        if path.resolve() != studio.ROOT.resolve() / str(user_id) / identity:
            raise HTTPException(404, '视频草案不存在')
        record = studio.read(path)
        shot = studio._find_shot(record, shot_id)
        diagnostic = shot.get('h3_prompt_diagnostic')
        if not isinstance(diagnostic, dict) or not diagnostic:
            raise HTTPException(404, '本镜尚无 H3 校验失败诊断')
        try:
            output = _h3_diagnostic_asset(path, diagnostic.get('file'))
        except ValueError as exc:
            raise HTTPException(400, 'H3 诊断文件路径无效') from exc
        if not output.is_file():
            raise HTTPException(404, 'H3 诊断文件不存在')
        return FileResponse(output, media_type='application/json', filename='h3-diagnostic.json',
                            headers={'Cache-Control': 'private, no-store',
                                     'X-Content-Type-Options': 'nosniff'})


@router.get('/{identity}/videos/{shot_id}')
def video(identity: str, shot_id: str, request: Request, original: bool = False):
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        shot = studio._find_shot(record, shot_id)
        if shot.get('video_status') != 'completed':
            raise HTTPException(404, '本镜视频尚未完成下载')
        try:
            output = _asset(path, str(shot.get('video') if original else shot.get('narration_preview') or shot.get('video') or ''))
        except ValueError as exc:
            raise HTTPException(404, '视频资产不存在') from exc
        if not output.is_file() or output.stat().st_size <= 0:
            raise HTTPException(404, '视频文件缺失，请检查本地任务目录')
        return FileResponse(output, media_type='video/mp4', filename=f'{shot_id}.mp4',
                            content_disposition_type='inline', headers={'Cache-Control': 'no-cache'})


@router.get('/{identity}/videos/{shot_id}/dub-timeline')
def dub_timeline(identity: str, shot_id: str, request: Request):
    from .video_manual_dub import basis, effective_clips
    from .video_export import _source, probe_media_duration
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        shot = studio._find_shot(record, shot_id)
        data = (shot.get('video_request') or {}).get('external_dialogue')
        if shot.get('video_status') != 'completed' or not data:
            raise HTTPException(409, '请先完成本镜视频及独立配音')
        source = _source(path, shot)
        duration = probe_media_duration(source)
        try:
            clips = effective_clips(shot, source, data, duration)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        names = {s['character_id']: s['name'] for s in (shot.get('native_dialogue') or {}).get('speakers', [])}
        names.update({s['character_id']: s['name'] for s in data['plan']['speakers']})
        return dict(basis=basis(source, data), duration=duration,
                    manual=bool(shot.get('manual_dub_timing')), revision=record['revision'],
                    clips=[dict(index=i, name=names.get(c['character_id'], '旁白' if c['kind']=='narrator' else '角色'),
                                text=c['text'], kind=c['kind'], start=c['start'], duration=c['duration'],
                                original_start=data['clips'][i]['start']) for i,c in enumerate(clips)])


@router.get('/{identity}/videos/{shot_id}/dub-clips/{index}')
def dub_clip(identity: str, shot_id: str, index: int, request: Request):
    from .video_external_dialogue import verified
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        shot = studio._find_shot(studio.read(path), shot_id)
        clips = ((shot.get('video_request') or {}).get('external_dialogue') or {}).get('clips', [])
        if shot.get('video_status') != 'completed' or not 0 <= index < len(clips):
            raise HTTPException(404, '本镜配音不存在')
        try:
            output = verified(path, clips[index])
        except (ValueError, OSError) as exc:
            raise HTTPException(404, '本镜配音资产不可用') from exc
        return FileResponse(output, media_type='audio/wav',
                            headers={'Cache-Control': 'private, no-cache'})


@router.post('/{identity}/videos/{shot_id}/dub-timeline')
def save_dub_timeline(identity: str, shot_id: str, data: ManualDubTiming, request: Request):
    from .video_manual_dub import basis, place
    from .video_export import _source, probe_media_duration
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        studio.editable(record, data.revision)
        shot = studio._find_shot(record, shot_id)
        exact = (shot.get('video_request') or {}).get('external_dialogue')
        if record.get('status') not in VIDEO_GENERATION_ENTRY_STAGES or shot.get('video_status') != 'completed' or not exact:
            raise HTTPException(409, '请在生成结束后调整本镜配音')
        key = (str(path), shot_id)
        if key in NARRATION_PREVIEWS:
            raise HTTPException(409, '本镜正在合成配音，请等待完成')
        source = _source(path, shot)
        current_basis = basis(source, exact)
        if current_basis != data.basis:
            raise HTTPException(409, '本镜素材已变化，请重新打开音轨调整')
        try:
            if not data.reset:
                place(exact['clips'], data.starts, probe_media_duration(source))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        snapshot = copy.deepcopy(shot)
        if data.reset:
            snapshot.pop('manual_dub_timing', None)
        else:
            snapshot['manual_dub_timing'] = dict(basis=current_basis, starts=list(data.starts))
        NARRATION_PREVIEWS.add(key)
    try:
        from .video_external_dialogue import preview
        try:
            result = preview(path, snapshot)
        except Exception as exc:
            raise HTTPException(409, '配音合成未完成，原预览保持不变：'+str(exc)[:1200]) from exc
        with studio.LOCK:
            latest = studio.read(path)
            studio.editable(latest, data.revision)
            live = studio._find_shot(latest, shot_id)
            if data.reset:
                live.pop('manual_dub_timing', None)
            else:
                live['manual_dub_timing'] = snapshot['manual_dub_timing']
            live.update(narration_preview=result, narration_preview_error='',
                        dialogue_alignment=snapshot.get('dialogue_alignment'))
            _invalidate_export_after_video_choice(latest, f'{shot_id}：已'+('恢复自动配音时序' if data.reset else '采用手动配音时序')+'；只重新合成，未生成声音或视频。')
            latest['revision'] += 1
            studio.save(path, latest)
            return latest
    finally:
        with studio.LOCK:
            NARRATION_PREVIEWS.discard(key)


@router.post('/{identity}/videos/{shot_id}/narration-preview')
def retry_narration_preview(identity: str, shot_id: str, data: VideoHistoryAction, request: Request):
    """Recheck and mix the existing frozen assets without generating new media."""
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        studio.editable(record, data.revision)
        shot = studio._find_shot(record, shot_id)
        exact=bool((shot.get('video_request') or {}).get('external_dialogue'))
        ready=shot.get('video_status')=='completed' or (exact and shot.get('video_status')=='failed' and shot.get('video_terminal'))
        if not ready or not (exact or (shot.get('video_request') or {}).get('external_narration')):
            raise HTTPException(409, '请先完成采用独立旁白路线的本镜视频')
        key = (str(path), shot_id)
        if key in NARRATION_PREVIEWS:
            raise HTTPException(409, '本镜正在检查并混合旁白，请稍候')
        snapshot = copy.deepcopy(shot)
        NARRATION_PREVIEWS.add(key)
    try:
        if exact:
            from .video_external_dialogue import preview
        else:
            from .video_external_narration import preview
        try:
            result, error = preview(path, snapshot), ''
        except Exception as exc:
            result, error = None, str(exc)[:1800]
        with studio.LOCK:
            record = studio.read(path)
            studio.editable(record, data.revision)
            shot = studio._find_shot(record, shot_id)
            if result:
                shot['narration_preview'] = result
                if exact:
                    shot.update(video_status='completed',video_error='',video_terminal=True,
                                video_version=_video_version(_asset(path,shot['video'])))
            shot['narration_preview_error'] = error
            if 'dialogue_alignment' in snapshot:shot['dialogue_alignment']=snapshot['dialogue_alignment']
            record['revision'] += 1
            record['logs'].append(f'{shot_id}：'+('旁白混合预览已更新；复用原视频和配音，未重新生成。' if result else '旁白混合未完成：'+error))
            studio.save(path, record)
            return record
    finally:
        with studio.LOCK:
            NARRATION_PREVIEWS.discard(key)


@router.get('/{identity}/videos/{shot_id}/history/{index}')
def historical_video(identity: str, shot_id: str, index: int, request: Request):
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        shot = studio._find_shot(record, shot_id)
        _, output = _history_video(path, shot, index)
        return FileResponse(output, media_type='video/mp4', filename=f'{shot_id}-history-{index + 1}.mp4',
                            content_disposition_type='inline', headers={'Cache-Control': 'no-cache'})


@router.post('/{identity}/videos/{shot_id}/history/{index}/adopt')
def adopt_historical_video(identity: str, shot_id: str, index: int,
                           data: VideoHistoryAction, request: Request):
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        studio.editable(record, data.revision)
        if record.get('status') not in VIDEO_GENERATION_ENTRY_STAGES:
            raise HTTPException(409, '请在动态镜头检查或成片阶段切换历史版本')
        shot = studio._find_shot(record, shot_id)
        if shot.get('kind') != 'video':
            raise HTTPException(409, '静态镜头没有历史视频版本')
        entry, output = _history_video(path, shot, index)
        _archive_current_video(path, shot, '切换历史版本前保留的当前片段')
        history = list(shot.get('video_history') or [])
        # The current version may have just been appended, so remove by the
        # original index and preserve the newly archived current version.
        selected = history.pop(index)
        shot['video_history'] = history
        for key in VIDEO_VERSION_FIELDS:
            if key in selected:
                shot[key] = copy.deepcopy(selected[key])
            else:
                shot.pop(key, None)
        shot.update(video_status='completed', video_error='', video_terminal=True,
                    video_resume_available=False, video_not_submitted=False,
                    video_execution_started=True,
                    video_version=str(selected.get('video_version') or _video_version(output)))
        _invalidate_export_after_video_choice(
            record, f'{shot_id}：已采用历史动态片段；其他镜头、配音和分镜保持不变，请重新合成成片。')
        record['revision'] += 1
        studio.save(path, record)
        return record


@router.delete('/{identity}/videos/{shot_id}/history/{index}')
def delete_historical_video(identity: str, shot_id: str, index: int,
                            revision: int, request: Request):
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        studio.editable(record, revision)
        shot = studio._find_shot(record, shot_id)
        _, output = _history_video(path, shot, index)
        history = list(shot.get('video_history') or [])
        history.pop(index)
        shot['video_history'] = history
        # Attempts are immutable directories. Remove an old attempt only when
        # no current/history entry references anything inside it.
        attempt_dir = output.parent.resolve()
        shot_root = (path / 'assets' / 'videos' / str(shot_id)).resolve()
        referenced = {str(shot.get('video') or '')}
        referenced.update(str(item.get('video') or '') for item in history if isinstance(item, dict))
        still_used = any(value and attempt_dir in _asset(path, value).parents
                         for value in referenced if value)
        if not still_used and shot_root in attempt_dir.parents:
            shutil.rmtree(attempt_dir, ignore_errors=True)
        record.setdefault('logs', []).append(f'{shot_id}：已删除一个未采用的历史动态片段。')
        record['revision'] += 1
        studio.save(path, record)
        return record


@router.post('/{identity}/videos/{shot_id}/upload')
async def upload_replacement_video(identity: str, shot_id: str, request: Request,
                                   revision: int = Form(...), file: UploadFile = File(...)):
    if Path(file.filename or '').suffix.lower() != '.mp4':
        raise HTTPException(400, '目前仅支持上传 MP4 视频')
    user_id = studio.require_user(request)['id']
    path = studio.directory(user_id, identity)
    with studio.LOCK:
        record = studio.read(path)
        studio.editable(record, revision)
        if record.get('status') not in VIDEO_GENERATION_ENTRY_STAGES:
            raise HTTPException(409, '请在动态镜头检查或成片阶段上传替换视频')
        shot = studio._find_shot(record, shot_id)
        if shot.get('kind') != 'video':
            raise HTTPException(409, '静态镜头不能上传动态片段')
        attempts = [int(shot.get('video_attempt') or 0)]
        attempts.extend(int(item.get('video_attempt') or 0) for item in shot.get('video_history') or []
                        if isinstance(item, dict))
        attempt = max(attempts, default=0) + 1
        target_dir = _asset(path, f'assets/videos/{shot_id}/attempt_{attempt:03d}')
        target_dir.mkdir(parents=True, exist_ok=False)
        temporary = target_dir / '.uploading.mp4'
    maximum = 2 * 1024 * 1024 * 1024
    size = 0
    try:
        with temporary.open('wb') as stream:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise HTTPException(413, '上传视频不能超过 2GB')
                stream.write(chunk)
        from .pipeline import probe_media_duration
        duration = probe_media_duration(temporary)
        output = target_dir / 'clip.mp4'
        temporary.replace(output)
        with studio.LOCK:
            latest = studio.read(path)
            studio.editable(latest, revision)
            live = studio._find_shot(latest, shot_id)
            _archive_current_video(path, live, '上传本地替换片段前保留的当前版本')
            relative = str(output.relative_to(path.resolve())).replace('\\', '/')
            live.update(video_attempt=attempt, video_backend='upload', video_task_id='',
                        video_state_file='', video_request={
                            'backend': 'upload', 'filename': Path(file.filename or 'clip.mp4').name,
                            'duration': round(duration, 3), 'use_duration': live.get('duration'),
                        }, video=relative, video_status='completed', video_error='',
                        video_version=_video_version(output), video_terminal=True,
                        video_resume_available=False, video_not_submitted=False,
                        video_execution_started=True)
            _invalidate_export_after_video_choice(
                latest, f'{shot_id}：已上传本地视频作为当前片段；原片已进入历史，其他镜头保持不变。')
            latest['revision'] += 1
            studio.save(path, latest)
            return latest
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    finally:
        await file.close()
