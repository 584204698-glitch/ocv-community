"""Pure grouping for bounded reuse of checked core frames, without choosing files.

This does not approve images or dispatch work. Runtime must verify the source
pixels, quality evidence and dependency version before using a proposed edge.
"""
from __future__ import annotations

import hashlib
import json

from .video_scene_references import enabled


def _unique(rows, key, label):
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get(key) for row in rows):
        raise ValueError(f'{label}缺少稳定标识')
    values = [row[key] for row in rows]
    if any(not isinstance(value, str) for value in values) or len(set(values)) != len(values):
        raise ValueError(f'{label}标识重复或无效')
    return sorted(rows, key=lambda row: row[key])


def reference_signature(record, shot):
    """Exact declared opening compatibility; it does not prove matching pixels."""
    props = shot.get('continuity_props')
    states = shot.get('continuity_states')
    cast = shot.get('cast_plan')
    if not (shot.get('scene_reference_id') and props and states and cast):
        return None
    props = _unique(props, 'id', '道具')
    states = _unique(states, 'prop_id', '道具状态')
    characters = _unique(cast.get('characters'), 'project_character_id', '角色')
    identities = []
    for row in characters:
        if row.get('presence') not in {'opening', 'depicted', 'later', 'offscreen'}:
            raise ValueError('连续性参考缺少角色入场时机')
        if row['presence'] in {'opening', 'depicted'}:
            if not row.get('identity_sha256'):
                raise ValueError('连续性参考缺少角色身份版本')
            identities.append({key: row[key] for key in ('project_character_id', 'identity_sha256', 'presence')})
    state_by_id = {row['prop_id']: row for row in states}
    appearance = []
    for row in props:
        state = state_by_id.get(row['id'])
        if not state:
            raise ValueError('连续性参考缺少道具开场状态')
        if row.get('presence') == 'opening' and state.get('visibility') == 'opening':
            if not row.get('appearance') or not row.get('appearance_en'):
                raise ValueError('连续性参考缺少道具外观版本')
            appearance.append({key: row[key] for key in ('id', 'appearance', 'appearance_en')})
    if not appearance:
        return None
    opening = []
    for row in states:
        state = row.get('opening')
        if (row.get('visibility') not in {'opening', 'later', 'offscreen'}
                or not isinstance(state, dict) or any(not state.get(key) for key in ('id', 'description', 'description_en'))):
            raise ValueError('连续性参考的道具可见性或开场状态无效')
        opening.append(dict(prop_id=row['prop_id'], visibility=row['visibility'], state=state))
    references = shot.get('reference_ids', [])
    if (not isinstance(references, list) or any(not isinstance(v, str) for v in references)
            or len(set(references)) != len(references)):
        raise ValueError('连续性参考素材标识无效')
    identity = dict(version=1, project_id=record.get('id'), scene=shot['scene_reference_id'],
                    style=(record.get('settings') or {}).get('style'),
                    ratio=(record.get('settings') or {}).get('ratio'),
                    cast=identities, props=appearance, states=opening, references=sorted(references))
    if not identity['project_id']:
        raise ValueError('连续性参考缺少项目标识')
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _visible_appearance(shot):
    """Only props declared visible in the source/target opening can be matched."""
    if not (shot.get('scene_reference_id') and shot.get('continuity_props') and shot.get('continuity_states')):
        return []
    props = _unique(shot['continuity_props'], 'id', '道具')
    states = _unique(shot['continuity_states'], 'prop_id', '道具状态')
    state_by_id = {row['prop_id']: row for row in states}
    for row in states:
        state = row.get('opening')
        if (row.get('visibility') not in {'opening', 'later', 'offscreen'}
                or not isinstance(state, dict) or any(not state.get(key) for key in ('id', 'description', 'description_en'))):
            raise ValueError('连续性参考的道具可见性或开场状态无效')
    result = []
    for prop in props:
        state = state_by_id.get(prop['id'])
        if not state:
            raise ValueError('连续性参考缺少道具开场状态')
        if prop.get('presence') == 'opening' and state['visibility'] == 'opening':
            if any(not isinstance(prop.get(key), str) or not prop[key].strip()
                   for key in ('appearance', 'appearance_en')):
                raise ValueError('连续性参考缺少道具外观版本')
            result.append({key: prop[key] for key in ('id', 'appearance', 'appearance_en')})
    return result


def appearance_signature(record, shot, source):
    """Match all target-visible prop identities, never mutable state or cast."""
    if not shot.get('scene_reference_id') or shot['scene_reference_id'] != source.get('scene_reference_id'):
        return None
    appearance = _visible_appearance(shot)
    source_props = {row['id']: row for row in _visible_appearance(source)}
    if not appearance or any(source_props.get(row['id']) != row for row in appearance):
        return None
    if not record.get('id'):
        raise ValueError('连续性参考缺少项目标识')
    identity = dict(version=1, mode='appearance_only', project_id=record['id'],
                    scene=shot['scene_reference_id'], style=(record.get('settings') or {}).get('style'),
                    ratio=(record.get('settings') or {}).get('ratio'), props=appearance)
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def plan_core_reference_groups(record):
    if not record.get('native_story') or not enabled(record):
        return []
    shots = record.get('shots', [])
    _unique(shots, 'id', '镜头')
    first_by_signature = {}
    roots = []
    result = []
    for shot in shots:
        signature = reference_signature(record, shot)
        source = first_by_signature.get(signature) if signature else None
        mode = 'opening'
        opening_signature = signature
        if source is None:
            for candidate in roots:
                matched = appearance_signature(record, shot, candidate)
                if matched:
                    source, signature, mode = candidate['id'], matched, 'appearance_only'
                    break
        result.append(dict(shot_id=shot['id'], source_shot_id=source, signature=signature, mode=mode))
        # Keep every proposed dependency one hop deep, in the existing ordered
        # worker group; a referenced image is never a new group source.
        if source is None and not shot.get('core_reference_used'):
            roots.append(shot)
            if opening_signature:
                first_by_signature[opening_signature] = shot['id']
    return result
