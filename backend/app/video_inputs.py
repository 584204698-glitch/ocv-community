"""Owner-scoped video inputs, with explicit choices ahead of cast-plan defaults."""
from __future__ import annotations

import copy
import hashlib
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import video_studio as studio
from .comfyui_bridge import _profiles, expand_h3_reference_capacity, validate_video_inputs, video_profile
from .video_character_coverage import CharacterCoverageMutation, build_character_review, character_coverage
from .video_project_cast import ProjectCastMutation, build_project_cast, project_cast_view
from .video_dialogue import VoiceBindingMutation, bind_voice, voice_view
from .video_cast_planning import (normalize_cast_plan, project_asset_catalog,
                                  validate_cast_plan, verified_planning_cast)

router = APIRouter(prefix='/api/video-studio')
EDIT_STAGES = {'image_review', 'video_generation_ready', 'video_review', 'export_failed', 'completed'}
INPUT_MODES = {'single_first', 'first_last', 'composition_reference'}


class VideoInputSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    input_mode: Literal['single_first', 'first_last', 'composition_reference'] = 'single_first'
    profile_id: str = Field(default='', max_length=80)
    end_frame_reference_id: str = Field(default='', max_length=160)
    reference_ids: list[str] = Field(default_factory=list, max_length=8)

    @field_validator('reference_ids')
    @classmethod
    def unique_references(cls, values):
        if len(set(values)) != len(values) or any(not value.strip() or len(value) > 160 for value in values):
            raise ValueError('参考图必须有有效且不重复的标识')
        return values


class VideoInputMutation(VideoInputSelection):
    revision: int = Field(ge=0)


def selection_for(shot):
    return VideoInputSelection.model_validate(shot.get('video_input_selection') or {}).model_dump()


def effective_profile_id(record, shot):
    return selection_for(shot)['profile_id'] or str(record.get('creation_parameters', {}).get('comfyui_profile_id') or '')


def effective_selection(path, record, shot):
    """Derive inputs without persisting adoption or claiming pixel/prose review.

    A saved choice, even an empty/default one, belongs to the user. Only new
    cast-aware composition shots inherit all planned identities. The directory
    supplies the owner; a saved cast cannot authorize its own account scope.
    """
    selection = selection_for(shot)
    if 'video_input_selection' in shot:
        return selection
    parameters = record.get('creation_parameters', {})
    if (parameters.get('video_generation_backend') != 'comfyui'
            or (record.get('project_cast') is None and shot.get('cast_plan') is None)):
        return selection
    try:
        owner = int(path.parent.name)
    except (ValueError, TypeError) as exc:
        raise ValueError('视频输入的项目账号目录无效，请重新读取项目') from exc
    if owner < 1 or path.name != record['id']:
        raise ValueError('视频输入目录与当前账号或项目不一致，请重新读取项目')
    profile = video_profile(owner, effective_profile_id(record, shot))
    if profile.get('video_input_mode') != 'composition_reference':
        return selection
    cast = verified_planning_cast(owner, path, record, project_asset_catalog(record))
    plan = shot.get('cast_plan')
    if cast is None and plan is None:
        return selection  # Empty legacy project bindings are not a shot plan.
    if plan is None:
        raise ValueError('本镜尚无角色出场计划，无法自动选择视频角色参考；请更新镜头设计或手动选择视频输入')
    if cast is None:
        plan = normalize_cast_plan(plan)
        if plan['project_id'] != record['id'] or plan['characters']:
            raise ValueError('本镜角色计划引用的项目身份已移除或不属于本项目，请更新镜头设计')
        references = []
    else:
        plan = validate_cast_plan(cast, plan)
        by_id = {row['project_character_id']: row for row in cast['characters']}
        # Core-image references intentionally omit later entrants. Video
        # identity inputs must instead follow the complete, validated plan.
        references = list(dict.fromkeys(by_id[row['project_character_id']]['reference_id']
                                        for row in plan['characters']))
    if len(references) > 8:
        raise ValueError(f'本镜角色计划需要 {len(references)} 张身份参考，超过视频输入的 8 张上限；请拆分镜头或调整角色计划')
    validate_video_inputs(profile, input_mode='composition_reference',
                          reference_count=len(references), has_last=False)
    return VideoInputSelection(input_mode='composition_reference', reference_ids=references).model_dump()


def asset_rows(record):
    # IDs refer to this project's copied assets, never caller-supplied paths.
    return [*record.get('references', []), *record.get('redraw_references', []),
            *record.get('video_input_assets', [])]


def reference_path(path, row):
    target = (path / str((row or {}).get('file') or '')).resolve()
    if not row or path.resolve() not in target.parents or not target.is_file():
        raise ValueError('所选视频参考图不存在，请重新选择或上传')
    return target


def resolve_video_inputs(path, record, shot):
    """Resolve exactly what will be frozen; never silently drop extra pictures."""
    if shot.get('image_status') != 'completed' or shot.get('design_needs_review'):
        raise ValueError('请先完成并确认本镜核心分镜图')
    core = studio._storyboard_image_path(path, shot)
    if not core.is_file() or path.resolve() not in core.resolve().parents:
        raise ValueError('本镜核心分镜图不存在')
    selection = effective_selection(path, record, shot)
    mode = selection['input_mode']
    tail_id, identities = selection['end_frame_reference_id'], selection['reference_ids']
    if mode == 'single_first' and (tail_id or identities):
        raise ValueError('首帧模式不能接收尾帧或额外参考，请明确更换模式')
    if mode == 'first_last' and (not tail_id or identities):
        raise ValueError('首尾帧模式需要一张已采用尾帧，不接收额外角色参考')
    if mode == 'composition_reference' and tail_id:
        raise ValueError('多图参考使用构图锚点，不能同时承诺锁定尾帧')
    catalog = {str(row['id']): row for row in asset_rows(record)}
    result = [{'path': core, 'id': shot['id'], 'name': '本镜核心图', 'label': '<Picture 1>',
               'role': 'composition' if mode == 'composition_reference' else 'first_frame'}]
    if tail_id:
        row = catalog.get(tail_id)
        result.append({'path': reference_path(path, row), 'id': tail_id, 'name': row.get('name') or '尾帧',
                       'label': 'last_frame', 'role': 'last_frame'})
    for index, identity in enumerate(identities, 2):
        row = catalog.get(identity)
        result.append({'path': reference_path(path, row), 'id': identity, 'name': row.get('name') or f'参考图 {index}',
                       'label': f'<Picture {index}>', 'role': 'identity_reference'})
    return selection, result


def validate_shot_inputs(user_id, path, record, shot, *, check_coverage=True):
    selection, inputs = resolve_video_inputs(path, record, shot)
    profile = video_profile(user_id, effective_profile_id(record, shot))
    validate_video_inputs(profile, input_mode=selection['input_mode'],
                          reference_count=len(selection['reference_ids']),
                          has_last=bool(selection['end_frame_reference_id']))
    if check_coverage:
        coverage = character_coverage(user_id, path, record, shot, selection, asset_rows(record))
        if coverage['status'] in {'partial', 'stale'}:
            raise ValueError(coverage['message'])
    return selection, inputs, profile


def _asset_view(record, row, path):
    try:
        reference_path(path, row)
        available = True
    except ValueError:
        available = False
    return {'id': row['id'], 'name': row.get('name') or row.get('label') or row['id'],
            'kind': 'image', 'available': available,
            'url': f'/api/video-studio/{quote(record["id"], safe="")}/video-input-assets/{quote(str(row["id"]), safe="")}' }


def input_view(user_id, path, record, shot):
    from .video_prompt_inputs import prompt_sync_view
    blockers = []
    prompt_sync = prompt_sync_view(user_id, path, record, shot)
    selection = selection_for(shot)
    try:
        selection = effective_selection(path, record, shot)
        coverage = character_coverage(user_id, path, record, shot, selection, asset_rows(record))
    except (ValueError, OSError, HTTPException) as exc:
        message = str(getattr(exc, 'detail', exc))
        blockers.append(message)
        # The editable fallback is not a valid effective input selection.
        # Never reuse a covered declaration when its inputs cannot resolve.
        coverage = {'status': 'unknown', 'message': message, 'characters': [],
                    'suggested_reference_ids': [], 'manual_prompt_warning': '',
                    'review': {'characters': []}}
    try:
        validate_shot_inputs(user_id, path, record, shot, check_coverage=False)
    except (ValueError, OSError, HTTPException) as exc:
        message = str(getattr(exc, 'detail', exc))
        if message not in blockers:
            blockers.append(message)
    inputs_valid = not blockers
    if coverage['status'] in {'partial', 'stale'}:
        blockers.append(coverage['message'])
    if prompt_sync['status'] == 'stale':
        blockers.append(prompt_sync['message'])
    prompt_sync['can_sync'] = bool(prompt_sync['can_sync'] and inputs_valid and record['status'] in EDIT_STAGES
        and record.get('creation_parameters', {}).get('video_generation_backend') == 'comfyui'
        and shot.get('kind') == 'video')
    profiles = [{'id': row['id'], 'name': row.get('name') or row['id'],
                 'input_mode': row.get('video_input_mode') or 'single_first',
                 'reference_capacity': len(row.get('mappings', {}).get('reference_images') or [])}
                for saved in _profiles(user_id) if saved.get('kind') == 'video'
                for row in [expand_h3_reference_capacity(saved)]]
    previous = shot.get('video_request') or {}
    manifest = previous.get('input_manifest')
    last_request = None if not previous else {
        'input_mode': previous.get('input_mode') or 'single_first',
        'inputs': [{key: row.get(key, '') for key in ('role', 'label', 'name')} for row in (manifest or [])],
        'legacy': not bool(manifest),
    }
    return {'project_id': record['id'], 'shot_id': shot['id'], 'revision': record['revision'],
            'selection': selection, 'assets': [_asset_view(record, row, path) for row in asset_rows(record)],
            'profiles': profiles, 'default_profile_id': record.get('creation_parameters', {}).get('comfyui_profile_id') or '',
            'core_image': {'available': shot.get('image_status') == 'completed',
                           'url': f'/api/video-studio/{record["id"]}/images/{shot["id"]}?v={shot.get("image_version", "")}'},
            'blockers': blockers, 'last_request': last_request, 'coverage': coverage, 'prompt_sync': prompt_sync}


def cast_view(owner, path, record):
    try:
        cast = project_cast_view(owner, path, record, asset_rows(record))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'project_id': record['id'], 'revision': record['revision'], **cast,
            'assets': [_asset_view(record, row, path) for row in asset_rows(record)]}


@router.get('/{identity}/project-cast')
def get_project_cast(identity: str, request: Request):
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        return cast_view(owner, path, record)


@router.put('/{identity}/project-cast')
def save_project_cast(identity: str, data: ProjectCastMutation, request: Request):
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        studio.editable(record, data.revision)
        try:
            candidate = build_project_cast(owner, path, record, asset_rows(record), data.characters)
            from .video_cast_planning import retired_character_references
            retired = retired_character_references(record, candidate)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if candidate != record.get('project_cast'):
            # Editing identity resources is not a shot visibility confirmation,
            # a media submission, or permission to rewrite already-made clips.
            record['project_cast'] = candidate
            if retired or 'retired_character_reference_ids' in record:
                record['retired_character_reference_ids'] = retired
            record['revision'] += 1
            record.setdefault('logs', []).append('已保存项目角色资料；关联镜头在下次生成前重新核对身份依据。')
            studio.save(path, record)
        return cast_view(owner, path, record)


@router.get('/{identity}/project-voices')
def get_project_voices(identity: str, request: Request):
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        try:
            return voice_view(owner, record)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, '角色音色记录不可用，请核对当前项目') from exc


@router.put('/{identity}/project-voices')
def save_project_voice(identity: str, data: VoiceBindingMutation, request: Request):
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        studio.editable(record, data.revision)
        try:
            candidate = bind_voice(owner, record, data.character_id, data.voice_id, voice_mode=data.voice_mode)
        except (OSError, ValueError) as exc:
            # OSError can contain private server paths; do not expose them.
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError)
                                else '参考声音不存在，请先试听并采用本账号的音色') from exc
        if candidate != record.get('project_voices'):
            record['project_voices'] = candidate
            affected = []
            for shot in record.get('shots', []):
                frozen_plan = ((shot.get('video_request') or {}).get('dialogue') or {}).get('plan') or {}
                if any(turn.get('character_id') == data.character_id for turn in frozen_plan.get('turns', [])):
                    shot['dialogue_stale'] = True
                    affected.append(shot['id'])
            if affected and record.get('status') == 'completed':
                record['status'] = 'video_review'
            record['revision'] += 1
            record.setdefault('logs', []).append('已保存角色音色绑定；历史视频保留，新对话生成使用当前绑定。')
            if affected:
                record['logs'].append('以下镜头的旧声音已过期，请局部重做后合成：' + '、'.join(affected))
            studio.save(path, record)
        return voice_view(owner, record)


def _editable(record, revision):
    studio.editable(record, revision)
    if record['status'] not in EDIT_STAGES:
        raise HTTPException(409, '请先完成核心图，再选择视频输入')
    if record.get('creation_parameters', {}).get('video_generation_backend') != 'comfyui':
        raise HTTPException(400, '该输入配置用于本地 ComfyUI 视频项目')


@router.get('/{identity}/shots/{shot_id}/video-inputs')
def get_inputs(identity: str, shot_id: str, request: Request):
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        return input_view(owner, path, record, studio._find_shot(record, shot_id))


@router.put('/{identity}/shots/{shot_id}/video-inputs')
def save_inputs(identity: str, shot_id: str, data: VideoInputMutation, request: Request):
    from .video_prompt_inputs import prompt_input_basis, input_fingerprint, adopt_prompt_basis
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        _editable(record, data.revision)
        shot = studio._find_shot(record, shot_id)
        if shot.get('kind') != 'video':
            raise HTTPException(400, '静态镜头不需要视频输入')
        candidate = copy.deepcopy(shot)
        candidate['video_input_selection'] = data.model_dump(exclude={'revision'})
        try:
            validate_shot_inputs(owner, path, record, candidate, check_coverage=False)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if 'video_input_selection' in shot and selection_for(shot) == selection_for(candidate):
            return record
        try:
            old_basis = prompt_input_basis(owner, path, record, shot)
        except (ValueError, OSError):
            old_basis = None
        try:
            new_basis = prompt_input_basis(owner, path, record, candidate)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        same_inputs = old_basis is not None and input_fingerprint(old_basis) == input_fingerprint(new_basis)
        pin_current = ('video_input_selection' not in shot and same_inputs
                       and effective_profile_id(record, shot) == effective_profile_id(record, candidate))
        if not same_inputs:
            shot['video_prompt_inputs_stale'] = True
        shot['video_input_selection'] = candidate['video_input_selection']
        if not same_inputs:
            adopt_prompt_basis(shot,new_basis,source='asset_replacement')
        if not pin_current:
            studio._invalidate_shot_video(record, shot, '本镜视频输入已修改')
            shot.pop('h3_prompt_source', None)
            # Follow the existing invalidation policy for a real input change;
            # pinning the same derived inputs does not replace frozen work.
            if record['status'] in {'completed', 'export_failed'}:
                record['status'] = 'video_review'
        record['revision'] += 1
        note = ('已固定本镜当前视频输入；已有视频与提示词保持不变。' if pin_current else
                '已保存本镜视频输入；其他镜头保持不变。')
        record.setdefault('logs', []).append(f'{shot_id}：{note}')
        studio.save(path, record)
        return record


@router.post('/{identity}/shots/{shot_id}/video-inputs/confirm-prompts')
def confirm_input_prompts(identity: str, shot_id: str, data: studio.Review, request: Request):
    from .video_prompt_inputs import prompt_input_basis, adopt_prompt_basis, prompt_sync_view
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        _editable(record, data.revision)
        shot = studio._find_shot(record, shot_id)
        if shot.get('kind') != 'video' or not str(shot.get('action') or '').strip() or not str(shot.get('video_prompt') or '').strip():
            raise HTTPException(400, '请先保存完整的动态表达和视频提示词，再确认与当前图片一致')
        try:
            validate_shot_inputs(owner, path, record, shot, check_coverage=False)
            basis = prompt_input_basis(owner, path, record, shot)
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        status = prompt_sync_view(owner, path, record, shot, basis=basis)['status']
        if status == 'current':
            return record
        if status == 'stale':
            studio._invalidate_shot_video(record, shot, '已人工核对变化后的正文与视频图片')
            if record['status'] in {'completed', 'export_failed'}:
                record['status'] = 'video_review'
        adopt_prompt_basis(shot, basis, source='manual_review')
        record['revision'] += 1
        record.setdefault('logs', []).append(f'{shot_id}：用户已核对动态正文与当前视频图片一致；角色可见性仍需单独核对。')
        studio.save(path, record)
        return record


@router.put('/{identity}/shots/{shot_id}/character-coverage')
def save_character_coverage(identity: str, shot_id: str, data: CharacterCoverageMutation, request: Request):
    owner = int(studio.require_user(request)['id'])
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        _editable(record, data.revision)
        shot = studio._find_shot(record, shot_id)
        if shot.get('kind') != 'video':
            raise HTTPException(400, '静态镜头不需要视频角色覆盖确认')
        try:
            review = build_character_review(owner, path, record, shot, effective_selection(path, record, shot),
                                            asset_rows(record), data.characters)
        except (OSError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if shot.get('video_character_review') == review:
            return record
        # A declaration changes evidence, not the generated content or inputs.
        shot['video_character_review'] = review
        record['revision'] += 1
        record.setdefault('logs', []).append(f'{shot_id}：已保存本镜角色确认；视频输入保持不变。')
        studio.save(path, record)
        return record


@router.post('/{identity}/video-input-assets')
async def upload_input(identity: str, request: Request, revision: int = Form(...), file: UploadFile = File(...)):
    owner = int(studio.require_user(request)['id'])
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in {'.jpg', '.jpeg', '.png', '.webp'}:
        raise HTTPException(400, '参考图仅支持 JPG、JPEG、PNG 或 WebP')
    content = await file.read(30 * 1024 * 1024 + 1)
    if not content or len(content) > 30 * 1024 * 1024:
        raise HTTPException(400, '参考图大小需在 1 字节到 30 MB 之间')
    with studio.LOCK:
        path = studio.directory(owner, identity)
        record = studio.read(path)
        # A candidate image may be bound before the first storyboard plan.
        # Uploading never adopts a video input or starts media generation.
        studio.editable(record, revision)
        if len(record.get('video_input_assets', [])) >= 300:
            raise HTTPException(400, '本项目视频参考图已达 300 张，请复用已有素材')
        asset_id = 'vi_' + uuid.uuid4().hex
        target = path / 'assets' / 'video_inputs' / f'{asset_id}{suffix}'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        try:
            from PIL import Image
            with Image.open(target) as image:
                image.verify()
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise HTTPException(400, '上传文件不是有效图片') from exc
        row = {'id': asset_id, 'name': Path(file.filename or '视频参考图').name,
               'file': target.relative_to(path).as_posix(), 'sha256': hashlib.sha256(content).hexdigest()}
        record.setdefault('video_input_assets', []).append(row)
        record['revision'] += 1
        studio.save(path, record)
        return {'project': record, 'asset': _asset_view(record, row, path)}


@router.get('/{identity}/video-input-assets/{asset_id}')
def input_image(identity: str, asset_id: str, request: Request):
    with studio.LOCK:
        path = studio.directory(studio.require_user(request)['id'], identity)
        record = studio.read(path)
        row = next((row for row in asset_rows(record) if row['id'] == asset_id), None)
        try:
            return FileResponse(reference_path(path, row))
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
