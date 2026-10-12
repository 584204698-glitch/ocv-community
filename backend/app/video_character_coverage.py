"""Explicit per-shot character declarations and verifiable image coverage.

Names are display labels, never image lookup keys. A confirmation records the
actual source bytes; this module does not recognize characters or call models.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .video_project_cast import project_cast_view


class CharacterDeclaration(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    id: str = Field(default='', max_length=160)
    name: str = Field(min_length=1, max_length=160)
    presence: Literal['opening', 'later']
    reference_id: str = Field(default='', max_length=160)
    in_last_frame: bool = Field(default=False, strict=True)
    project_character_id: str = Field(default='', max_length=160)
    core_identity_confirmed: bool = Field(default=False, strict=True)

    @model_validator(mode='after')
    def core_identity_requires_opening(self):
        if self.core_identity_confirmed and self.presence != 'opening':
            raise ValueError('只有已核对开场可见的角色才能确认核心图身份')
        return self

    @field_validator('id', 'reference_id', 'project_character_id', mode='before')
    @classmethod
    def empty_identifier(cls, value):
        return '' if value is None else value


class CharacterCoverageMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    # Omission must not become an explicit no-character declaration.
    characters: list[CharacterDeclaration] = Field(max_length=16)

    @field_validator('characters')
    @classmethod
    def unique_identifiers(cls, values):
        identifiers = [value.id for value in values if value.id]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError('角色标识不能重复')
        linked = [value.project_character_id for value in values if value.project_character_id]
        if len(linked) != len(set(linked)):
            raise ValueError('同一项目角色在本镜只需确认一次')
        return values


_BASIS_FIELDS = (
    'id', 'kind', 'action', 'video_prompt', 'motion_plan', 'image_prompt',
    'duration', 'generation_duration', 'start', 'end', 'slide_ids', 'intent',
    'visual_description', 'visual_design', 'image', 'image_version',
    'image_status', 'design_needs_review',
)
_EDIT_FIELDS = ('id', 'name', 'presence', 'reference_id', 'in_last_frame', 'project_character_id',
                'core_identity_confirmed')


def _fingerprint(record, shot):
    basis = {
        'project_id': record['id'],
        'shot': {key: shot.get(key) for key in _BASIS_FIELDS},
        'settings': {key: record.get('settings', {}).get(key)
                     for key in ('style', 'characters', 'world', 'ratio')},
    }
    return hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _asset(catalog, identity):
    matches = [row for row in catalog if str(row.get('id') or '') == identity]
    if len(matches) != 1:
        raise ValueError('角色或尾帧参考图不存在或标识重复，请重新选择本项目图片')
    return matches[0]


def _image_snapshot(path, relative, *, verify=False):
    root = Path(path).resolve()
    target = (root / str(relative or '')).resolve()
    if not relative or root not in target.parents or not target.is_file():
        raise ValueError('已确认的图片不存在或不属于本项目，请重新选择')
    if verify:
        try:
            with Image.open(target) as image:
                image.verify()
        except Exception as exc:
            raise ValueError('角色确认需要有效的图片文件') from exc
    return {'file': str(relative).replace('\\', '/'),
            'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}


def build_character_review(owner, path, record, shot, selection, catalog, declarations):
    """Capture only user-declared roles and their exact project-owned assets."""
    if shot.get('image_status') != 'completed' or shot.get('design_needs_review'):
        raise ValueError('请先完成并确认本镜核心分镜图，再确认角色')
    characters = [value.model_dump() for value in declarations]
    bindings = {}
    cast = ({row['id']: row for row in project_cast_view(owner, path, record, catalog)['characters']}
            if any(row['project_character_id'] for row in characters) else {})
    for row in characters:
        row['id'] = row['id'] or 'vc_' + uuid.uuid4().hex
        if row['project_character_id']:
            linked = cast.get(row['project_character_id'])
            if (not linked or linked['status'] != 'current' or row['name'] != linked['name']
                    or row['reference_id'] != linked['reference_id']):
                raise ValueError('关联的项目角色已变化或不可用，请重新选用角色并核对本镜图片')
            bindings[linked['id']] = linked['identity_sha256']
    core = _image_snapshot(path, shot.get('image'), verify=True)
    references = {}
    for row in characters:
        identity = row['reference_id']
        if identity and identity not in references:
            references[identity] = _image_snapshot(path, _asset(catalog, identity).get('file'), verify=True)
    tail = None
    if any(row['in_last_frame'] for row in characters):
        identity = selection['end_frame_reference_id']
        if selection['input_mode'] != 'first_last' or not identity:
            raise ValueError('只有已采用真实尾帧时，才能确认角色出现在尾帧')
        tail = {'reference_id': identity,
                **_image_snapshot(path, _asset(catalog, identity).get('file'), verify=True)}
    result = {'version': 1, 'source': 'user_confirmed',
            'owner_id': int(owner), 'project_id': record['id'], 'shot_id': shot['id'],
            'source_fingerprint': _fingerprint(record, shot), 'core_image': core,
            'last_frame': tail, 'reference_images': references, 'characters': characters}
    if bindings:
        result['project_characters'] = bindings
    return result


def character_coverage(owner, path, record, shot, selection, catalog):
    """Describe current saved inputs; unknown/stale declarations never prove coverage."""
    manual_warning = '' if record.get('creation_parameters', {}).get('comfyui_h3_prompt_agent') else (
        '当前未开启 H3 提示词转换；新增、替换或重排参考图后，请核对手写提示词的 Picture/Subject 对应关系。'
        '角色确认不会自动改写提示词。')
    result = {'status': 'unknown', 'message': '尚未确认整镜角色；旧记录或缺失信息不表示本镜无人。',
              'characters': [], 'suggested_reference_ids': [], 'manual_prompt_warning': manual_warning,
              'review': {'characters': []}}
    saved = shot.get('video_character_review')
    if not isinstance(saved, dict) or saved.get('version') != 1 or not isinstance(saved.get('characters'), list):
        return result
    try:
        declarations = CharacterCoverageMutation(revision=0, characters=saved['characters']).characters
    except ValueError:
        return result
    characters = [value.model_dump() for value in declarations]
    if any(not row['id'] for row in characters):
        return result
    result['review'] = {'characters': [{key: row[key] for key in _EDIT_FIELDS} for row in characters]}
    # A legacy or incomplete object is not an explicit empty confirmation.
    if (not isinstance(saved.get('core_image'), dict)
            or not isinstance(saved.get('reference_images'), dict)
            or not saved.get('source_fingerprint')
            or any(key not in saved for key in ('owner_id', 'project_id', 'shot_id', 'last_frame'))):
        return result
    stale = (saved['owner_id'] != int(owner) or saved['project_id'] != record['id']
             or saved['shot_id'] != shot['id'] or saved['source_fingerprint'] != _fingerprint(record, shot))
    cast = {}
    if any(row['project_character_id'] for row in characters):
        try:
            cast = {row['id']: row for row in project_cast_view(owner, path, record, catalog)['characters']}
        except ValueError:
            stale = True
    for row in characters:
        identity = row['project_character_id']
        if identity:
            linked = cast.get(identity)
            saved_bindings = saved.get('project_characters')
            if not isinstance(saved_bindings, dict):
                saved_bindings = {}
            stale |= (not linked or linked['status'] != 'current'
                or saved_bindings.get(identity) != linked['identity_sha256']
                or row['name'] != linked['name'] or row['reference_id'] != linked['reference_id'])
    try:
        stale |= saved['core_image'] != _image_snapshot(path, shot.get('image'))
    except (OSError, ValueError):
        stale = True
    reference_valid = {}
    for row in characters:
        identity = row['reference_id']
        if not identity or identity in reference_valid:
            continue
        try:
            reference_valid[identity] = (saved['reference_images'].get(identity)
                                         == _image_snapshot(path, _asset(catalog, identity).get('file')))
        except (OSError, ValueError):
            reference_valid[identity] = False
        stale |= not reference_valid[identity]
    tail_valid, tail_selected = False, False
    if any(row['in_last_frame'] for row in characters):
        tail = saved['last_frame']
        try:
            identity = str(tail.get('reference_id') or '') if isinstance(tail, dict) else ''
            tail_valid = (bool(identity)
                          and tail == {'reference_id': identity,
                                       **_image_snapshot(path, _asset(catalog, identity).get('file'))})
            tail_selected = (selection['input_mode'] == 'first_last'
                             and selection['end_frame_reference_id'] == identity)
        except (OSError, ValueError):
            tail_valid = False
        # Switching to references need not discard a valid user declaration.
        # A different active tail, however, has never been visually confirmed.
        stale |= not tail_valid
        stale |= selection['input_mode'] == 'first_last' and not tail_selected
    for row in characters:
        identity = row['reference_id']
        available = bool(identity and reference_valid.get(identity))
        covered, via, reason = False, '', ''
        if stale:
            reason = '角色确认的依据或图片已变化，请重新核对并保存。'
        elif row['presence'] == 'opening':
            covered, via = True, 'core'
        elif tail_selected and row['in_last_frame'] and tail_valid:
            covered, via = True, 'last_frame'
        elif (selection['input_mode'] == 'composition_reference' and available
              and identity in selection['reference_ids']):
            covered, via = True, 'reference'
        elif selection['input_mode'] == 'single_first':
            reason = '单首帧没有后入画角色的图片条件；请采用角色参考或确认含该角色的尾帧。'
        elif selection['input_mode'] == 'first_last':
            reason = '该角色未确认在尾帧出现；中途入画又离场的角色仍需单独的身份参考。'
        elif not identity:
            reason = '后入画角色尚未绑定实际角色参考图。'
        else:
            reason = '绑定的角色参考图尚未采用为本镜视频输入。'
        result['characters'].append({**row, 'reference_available': available,
                                     'covered': covered, 'via': via, 'reason': reason})
        if not stale and row['project_character_id']:
            linked = cast[row['project_character_id']]
            result['characters'][-1].update(identity_sha256=linked['identity_sha256'],
                                            constraints=linked['constraints'])
        if (not stale and available and identity not in selection['reference_ids']
                and identity not in result['suggested_reference_ids']):
            result['suggested_reference_ids'].append(identity)
    if stale:
        # An old checkbox must not be silently re-saved against new pixels.
        for row in result['review']['characters']:
            row['core_identity_confirmed'] = False
        for row in result['characters']:
            row['core_identity_confirmed'] = False
        result.update(status='stale', message='动作、提示词、项目角色或已确认图片已变化，原角色确认已过期；请重新核对并保存后再生成。')
    elif not characters:
        result.update(status='empty', message='已明确确认本镜没有可见角色。')
    elif all(row['covered'] for row in result['characters']):
        result.update(status='covered', message='已确认角色的图片输入已覆盖；这不保证生成结果的外观或动作一致。')
    else:
        missing = '、'.join(row['name'] for row in result['characters'] if not row['covered'])
        result.update(status='partial', message=f'角色输入尚未覆盖：{missing}。请补齐本镜输入后再生成。')
    return result
