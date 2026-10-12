"""Explicit project identities for planning; never claims pixel visibility."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .video_project_cast import (
    ProjectCastDeclaration, SOURCE, AUTO_SOURCE, _saved_characters, project_cast_view,
)


class CreationCastBinding(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    asset_id: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    constraints: str = Field(default='', max_length=2000)

    @field_validator('asset_id', 'name', 'constraints')
    @classmethod
    def ordinary_text(cls, value):
        return ProjectCastDeclaration.ordinary_text(value)


def creation_cast_bindings(bindings, reference_ids):
    if not isinstance(bindings, list) or len(bindings) > 6:
        raise ValueError('首次制作最多绑定 6 个固定角色')
    rows = [CreationCastBinding.model_validate(row).model_dump() for row in bindings]
    if rows and (not isinstance(reference_ids, list) or len(reference_ids) > 6
            or any(not isinstance(identity, str) or not identity.strip() or len(identity) > 180
                   for identity in reference_ids) or len(set(reference_ids)) != len(reference_ids)):
        raise ValueError('固定角色的参考图目录无效，请重新选择本次参考图')
    ids = [row['asset_id'] for row in rows]
    if len(set(ids)) != len(ids) or any(identity not in reference_ids for identity in ids):
        raise ValueError('固定角色必须选择本次已上传的参考图，每张图只能绑定一次')
    names = [row['name'] for row in rows]
    if len(set(names)) != len(names):
        raise ValueError('固定角色的称呼不能重复，请写成原文中可区分的姓名或称呼')
    return rows


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def _snapshot(record, rows):
    if not rows:
        return None
    result = {'version': 1, 'source': record['project_cast']['source'], 'project_id': record['id'], 'characters': [
        {'project_character_id': row['id'], **{key: row[key] for key in (
            'name', 'reference_id', 'constraints', 'reference_sha256', 'identity_sha256')}}
        for row in rows]}
    result['cast_sha256'] = _digest(result)
    return validate_planning_cast(result)


def saved_planning_cast(record):
    """Pure snapshot for fingerprints; verify bytes at the execution boundary."""
    saved = record.get('project_cast')
    if saved is None:
        return None
    owner = saved.get('owner_id') if isinstance(saved, dict) else None
    return _snapshot(record, _saved_characters(owner, record))


def verified_planning_cast(owner, path, record, catalog):
    rows = project_cast_view(owner, path, record, catalog)['characters']
    if any(row['status'] != 'current' for row in rows):
        raise ValueError('项目角色图片或资料已变化，请核对并保存角色后再规划')
    return _snapshot(record, rows)


def validate_planning_cast(value):
    if not isinstance(value, dict) or set(value) != {
            'version', 'source', 'project_id', 'characters', 'cast_sha256'}:
        raise ValueError('角色规划快照格式无效')
    if (type(value['version']) is not int or value['version'] != 1 or value['source'] not in {SOURCE, AUTO_SOURCE}
            or not isinstance(value['project_id'], str) or not value['project_id']):
        raise ValueError('角色规划快照来源无效')
    rows = value['characters']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
        raise ValueError('角色规划快照数量无效')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'project_character_id', 'name', 'reference_id',
                'constraints', 'reference_sha256', 'identity_sha256'}:
            raise ValueError('角色规划身份格式无效')
        declaration = ProjectCastDeclaration(id=row['project_character_id'], name=row['name'],
            reference_id=row['reference_id'], constraints=row['constraints']).model_dump()
        if any(declaration[key] != row['project_character_id' if key == 'id' else key]
               for key in declaration):
            raise ValueError('角色规划身份必须使用已保存的精确字段')
        identity = row['project_character_id']
        if not identity or identity in seen:
            raise ValueError('角色规划身份重复或为空')
        seen.add(identity)
        for key in ('reference_sha256', 'identity_sha256'):
            if not isinstance(row[key], str) or not re.fullmatch(r'[a-f0-9]{64}', row[key]):
                raise ValueError('角色规划身份指纹无效，请重新保存角色')
    if value['cast_sha256'] != _digest({key: val for key, val in value.items() if key != 'cast_sha256'}):
        raise ValueError('角色规划快照已变化')
    return copy.deepcopy(value)


def normalize_cast_plan(value):
    if not isinstance(value, dict) or set(value) != {'version', 'source', 'project_id', 'characters'}:
        raise ValueError('镜头角色计划格式无效')
    if (type(value['version']) is not int or value['version'] != 1 or value['source'] != 'director_plan'
            or not isinstance(value['project_id'], str) or not value['project_id']):
        raise ValueError('镜头角色计划来源无效')
    rows = value['characters']
    if not isinstance(rows, list) or len(rows) > 16:
        raise ValueError('镜头角色计划数量无效')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'project_character_id', 'presence', 'basis', 'identity_sha256'}:
            raise ValueError('镜头角色计划字段无效')
        identity = row['project_character_id']
        if not isinstance(identity, str) or not re.fullmatch(r'pc_[a-f0-9]{32}', identity) or identity in seen:
            raise ValueError('镜头项目角色标识无效或重复')
        seen.add(identity)
        if (row['presence'] not in ('opening', 'later', 'depicted') or not isinstance(row['basis'], str)
                or not row['basis'].strip() or len(row['basis']) > 2000
                or not isinstance(row['identity_sha256'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', row['identity_sha256'])):
            raise ValueError('镜头角色出场计划或身份依据无效')
    return copy.deepcopy(value)


def bind_planned_characters(snapshot, raw_list):
    cast = validate_planning_cast(snapshot)
    if not isinstance(raw_list, list) or len(raw_list) > 16:
        raise ValueError('请返回 planned_characters 数组；无项目角色入画时明确填 []')
    catalog = {row['project_character_id']: row for row in cast['characters']}
    rows = []
    for raw in raw_list:
        if not isinstance(raw, dict) or set(raw) != {'project_character_id', 'presence', 'basis'}:
            raise ValueError('角色出场计划只接受 project_character_id、presence 和 basis')
        identity = raw['project_character_id']
        if not isinstance(identity, str) or identity not in catalog:
            raise ValueError('角色出场计划引用了不存在的项目角色；必须使用提供的 pc_ID')
        rows.append({**raw, 'identity_sha256': catalog[identity]['identity_sha256']})
    return normalize_cast_plan({'version': 1, 'source': 'director_plan',
        'project_id': cast['project_id'], 'characters': rows})


def validate_cast_plan(snapshot, cast_plan):
    cast = validate_planning_cast(snapshot)
    plan = normalize_cast_plan(cast_plan)
    if plan['project_id'] != cast['project_id']:
        raise ValueError('镜头角色计划不属于本项目')
    identities = {row['project_character_id']: row['identity_sha256'] for row in cast['characters']}
    if any(identities.get(row['project_character_id']) != row['identity_sha256'] for row in plan['characters']):
        raise ValueError('镜头角色身份依据已变化，请更新本镜设计')
    return plan


def plan_reference_ids(snapshot, cast_plan, selected_refs):
    cast = validate_planning_cast(snapshot)
    plan = validate_cast_plan(cast, cast_plan)
    by_id = {row['project_character_id']: row for row in cast['characters']}
    all_refs = {row['reference_id'] for row in cast['characters']}
    opening = list(dict.fromkeys(by_id[row['project_character_id']]['reference_id']
        for row in plan['characters'] if row['presence'] in {'opening', 'depicted'}))
    if not isinstance(selected_refs, list) or any(not isinstance(ref, str) for ref in selected_refs):
        raise ValueError('镜头参考图选择无效')
    if any(ref in all_refs and ref not in opening for ref in selected_refs):
        raise ValueError('后入场或未计划入画的角色图不能作为开场核心图的角色参考；请保留其后续身份计划')
    result = list(dict.fromkeys([*selected_refs, *opening]))
    if len(result) > 8:
        raise ValueError('开场参考图超过镜头可用数量')
    return result


def retired_character_references(record, replacement=None):
    """Keep prior identity images available, but out of future planning inputs."""
    saved = record.get('retired_character_reference_ids', [])
    if not isinstance(saved, list) or any(not isinstance(v, str) or not v for v in saved):
        raise ValueError('历史角色参考图目录无效，请检查项目资料')
    retired = set(saved)
    if replacement is not None:
        old = record.get('project_cast')
        rows = old.get('characters', []) if isinstance(old, dict) else []
        if isinstance(rows, list):
            retired.update(row['reference_id'] for row in rows if isinstance(row, dict)
                           and isinstance(row.get('reference_id'), str) and row['reference_id'])
        retired.difference_update(row['reference_id'] for row in replacement['characters'])
    return sorted(retired)


def planning_references(record):
    """Original materials plus explicitly bound supplemental assets, by ID only."""
    all_rows = project_asset_catalog(record)
    retired = set(retired_character_references(record))
    rows = copy.deepcopy([row for row in record.get('references', []) if row['id'] not in retired])
    cast = saved_planning_cast(record)
    if not cast:
        return rows
    wanted = list(dict.fromkeys(row['reference_id'] for row in cast['characters']))
    ids = {row['id'] for row in rows}
    labels = {row.get('label', '') for row in rows}
    for identity in wanted:
        matches = [row for row in all_rows if row.get('id') == identity]
        if len(matches) != 1:
            raise ValueError('项目角色参考素材不存在或标识重复，请重新绑定')
        if identity in ids:
            continue
        row = copy.deepcopy(matches[0])
        index = 1
        while f'图{index}' in labels:
            index += 1
        row.update(label=f'图{index}', kind='character')
        labels.add(row['label']); ids.add(identity); rows.append(row)
    return rows


def project_asset_catalog(record):
    result = []
    for key in ('references', 'redraw_references', 'video_input_assets'):
        rows = record.get(key, [])
        if (not isinstance(rows, list) or any(not isinstance(row, dict)
                or not isinstance(row.get('id'), str) or not row['id'].strip() for row in rows)):
            raise ValueError('项目参考素材目录格式无效，请重新读取并核对素材')
        result.extend(rows)
    return result
