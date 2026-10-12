"""Track a shot's prose against actual video inputs, separately from pixel review."""
from __future__ import annotations

import hashlib
import io
import json

from PIL import Image


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def content_fingerprint(shot):
    fields = ('action', 'motion_plan', 'video_prompt', 'intent', 'duration', 'generation_duration', 'cast_plan')
    value = {key: shot.get(key) for key in fields}
    if shot.get('native_dialogue'):
        # Verbatim turns have their own frozen dialogue fingerprint. Changing
        # words/timing does not change which visual references were adopted.
        value['native_dialogue'] = {key: val for key, val in shot['native_dialogue'].items() if key != 'turns'}
    return _digest(value)


def prompt_input_basis(owner, path, record, shot):
    # Resolve the same owner-local assets used by the freeze path. Never accept
    # browser-supplied paths, hashes, picture roles or identity declarations.
    from .video_inputs import asset_rows, resolve_video_inputs
    from .video_project_cast import project_cast_view
    selection, inputs = resolve_video_inputs(path, record, shot)
    catalog = asset_rows(record)
    images = []
    for number, item in enumerate(inputs, 1):
        if number > 1 and sum(row.get('id') == item['id'] for row in catalog) != 1:
            raise ValueError('所选视频参考图标识重复或已失效，请重新选择')
        raw = item['path'].read_bytes()
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
        except Exception as exc:
            raise ValueError('所选视频输入不是有效图片，请检查图片后重试') from exc
        images.append({**{key: item[key] for key in ('id', 'name', 'role')},
                       'label': f'<Picture {number}>', 'sha256': hashlib.sha256(raw).hexdigest()})
    characters = []
    for row in project_cast_view(owner, path, record, catalog)['characters']:
        if row['status'] != 'current':
            raise ValueError('项目角色资料已变化，请先核对并保存角色绑定')
        labels = [image['label'] for image in images[1:]
                  if image['id'] == row['reference_id'] and image['role'] == 'identity_reference']
        characters.append({**{key: row[key] for key in
                              ('id', 'name', 'reference_id', 'constraints', 'identity_sha256')},
                           'video_picture_labels': labels,
                           'input_status': 'identity_reference_selected' if labels else 'project_bound_only'})
    return {'version': 1, 'input_mode': selection['input_mode'], 'images': images,
            'project_characters': characters}


def input_fingerprint(basis):
    return _digest(basis)


def adopt_prompt_basis(shot, basis, *, source='synchronized'):
    shot['video_prompt_input_basis'] = {'version': 1, 'input_fingerprint': input_fingerprint(basis),
                                      'content_fingerprint': content_fingerprint(shot), 'source': source}
    shot.pop('video_prompt_inputs_stale', None)


def prompt_sync_view(owner, path, record, shot, *, basis=None):
    try:
        basis = basis if basis is not None else prompt_input_basis(owner, path, record, shot)
    except (ValueError, OSError) as exc:
        return {'status': 'unavailable', 'message': str(exc), 'can_sync': False}
    saved = shot.get('video_prompt_input_basis')
    if shot.get('video_prompt_inputs_stale'):
        return {'status': 'stale', 'message': '视频图片或输入方式已改变，请按当前输入同步本镜提示词。', 'can_sync': True}
    if saved is None:
        return {'status': 'unknown', 'message': '旧提示词未记录图片依据，可按当前视频输入同步。', 'can_sync': True}
    if (not isinstance(saved, dict) or saved.get('version') != 1
            or saved.get('input_fingerprint') != input_fingerprint(basis)
            or saved.get('content_fingerprint') != content_fingerprint(shot)):
        return {'status': 'stale', 'message': '当前图片、角色资料或动态正文与上次同步不一致，请重新同步。', 'can_sync': True}
    if saved.get('source') == 'asset_replacement':
        return {'status':'retained','message':'已替换素材并保留提示词，未调用语言模型；新图片与动作是否相符尚未重新分析。','can_sync':True}
    return {'status': 'current', 'message': '本镜动态正文已对应当前视频图片；角色可见性仍以单独核对为准。', 'can_sync': True}


def assert_prompt_inputs_current(owner, path, record, shot):
    # Legacy records remain explicitly unknown, preserving existing workflows.
    # Once inputs were changed or a basis saved, stale prose cannot launch a new
    # task. Resuming an already-frozen task does not call this guard.
    if not shot.get('video_prompt_inputs_stale') and shot.get('video_prompt_input_basis') is None:
        return
    view = prompt_sync_view(owner, path, record, shot)
    if view['status'] not in {'current','retained'}:
        raise ValueError(view['message'])
