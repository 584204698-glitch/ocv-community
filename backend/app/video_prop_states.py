"""Explicit physical prop states across a native story's ordered shots."""
from __future__ import annotations

import copy
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LocalPropBasisError(ValueError):
    """A basis-field error with a known shot, safe for bounded local repair."""

    def __init__(self, index, message, related_indices=()):
        self.shot_ids = [f'shot_{i:03d}' for i in sorted({index, *related_indices}) if i > 0]
        super().__init__(f'shot_{index:03d}：{message}')


class State(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    # IDs are local references, not executable paths or prompt labels. A
    # meaningful identifier such as bell_high is as valid as state_high.
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    description: str = Field(min_length=4, max_length=700)
    description_en: str = Field(min_length=8, max_length=1000)

    @field_validator('description_en')
    @classmethod
    def english(cls, value):
        if any(c in value for c in '<>[]\n\r') or any(c.isalpha() and not c.isascii() for c in value):
            raise ValueError('道具状态的H3描述须为英文，不含提示词标签')
        return value


class Track(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    prop_id: str = Field(pattern=r'^prop_[a-z0-9_]{1,40}$')
    initial_state: str
    states: list[State] = Field(min_length=1, max_length=20)


class Beat(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    prop_id: str = Field(pattern=r'^prop_[a-z0-9_]{1,40}$')
    opening_state: str
    ending_state: str
    visibility: Literal['opening', 'later', 'offscreen']
    visibility_basis: str = Field(min_length=4, max_length=700)
    change_basis: str = Field(default='', max_length=1000)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    prop_id: str
    name: str
    opening: State
    ending: State
    visibility: Literal['opening', 'later', 'offscreen']
    visibility_basis: str
    change_basis: str


def normalize(rows):
    if not isinstance(rows, list) or len(rows) > 8:
        raise ValueError('道具跨镜状态格式无效')
    result = [Snapshot.model_validate(row).model_dump() for row in rows]
    if len({r['prop_id'] for r in result}) != len(result):
        raise ValueError('道具跨镜状态重复')
    return result


def compile_timeline(raw, props, shots):
    """Return None for legacy plans. Versioned plans must cover every prop/shot.

    Matching IDs prove declared continuity, not that the prose or pixels are
    correct. Semantic review and actual image/video inspection remain required.
    """
    if 'prop_state_tracks' not in raw:
        return None
    source = raw['prop_state_tracks']
    if not isinstance(source, list) or len(source) > 8:
        raise ValueError('道具状态轨迹格式无效')
    tracks = [Track.model_validate(t) for t in source]
    catalog = {p['id']: p for p in props}
    if len(tracks) != len(catalog) or {t.prop_id for t in tracks} != catalog.keys():
        raise ValueError('每个关键道具必须有且仅有一条状态轨迹')
    states, current = {}, {}
    for track in tracks:
        states[track.prop_id] = {s.id: s.model_dump() for s in track.states}
        if len(states[track.prop_id]) != len(track.states) or track.initial_state not in states[track.prop_id]:
            raise ValueError('道具状态重复或缺少初始状态')
        current[track.prop_id] = track.initial_state
    result = []
    compact = lambda s: ''.join(s.split())
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            raise ValueError(f'shot_{index:03d}：镜头状态必须是对象')
        entries = shot.get('prop_timeline')
        if not isinstance(entries, list) or len(entries) != len(catalog):
            raise ValueError(f'shot_{index:03d}：必须交代所有关键道具的状态，画外道具也不能省略')
        beats = [Beat.model_validate(b) for b in entries]
        if {b.prop_id for b in beats} != catalog.keys():
            raise ValueError(f'shot_{index:03d}：道具状态有重复或未知身份')
        resolved = []
        for beat in beats:
            if beat.opening_state != current[beat.prop_id]:
                raise LocalPropBasisError(index,
                    f'{catalog[beat.prop_id]["name"]}开场未承接上一镜结束状态，不能无动作换位置或消失',
                    related_indices=(index - 1,))
            if beat.ending_state not in states[beat.prop_id]:
                raise ValueError(f'shot_{index:03d}：道具结束状态不存在')
            changed = beat.opening_state != beat.ending_state
            if changed and (not beat.change_basis or not any(compact(beat.change_basis) in compact(str(shot.get(k) or ''))
                                                           for k in ('source_text', 'action'))):
                raise LocalPropBasisError(index, '道具状态变化须逐字引用本镜原稿或已设计动作作为依据')
            if not changed and beat.change_basis:
                raise LocalPropBasisError(index, '开场与结束状态相同时change_basis必须为空；镜内暂时变化后复原的动作保留在action/performance_beats，不要删除动作')
            resolved.append(dict(prop_id=beat.prop_id, name=catalog[beat.prop_id]['name'],
                opening=copy.deepcopy(states[beat.prop_id][beat.opening_state]),
                ending=copy.deepcopy(states[beat.prop_id][beat.ending_state]),
                visibility=beat.visibility, visibility_basis=beat.visibility_basis,
                change_basis=beat.change_basis))
            current[beat.prop_id] = beat.ending_state
        result.append(resolved)
    return result


IMAGE_MARKER = '\n【道具开场状态】'
VIDEO_MARKER = ' Prop state continuity: '


def repair_scope(previous, shot_ids):
    """State wording can change only when every consumer is being repaired."""
    selected, protected = {}, {}
    for index, shot in enumerate(previous.get('shots', []), 1):
        bucket = selected if f'shot_{index:03d}' in shot_ids else protected
        for beat in shot.get('prop_timeline', []):
            bucket.setdefault(beat['prop_id'], set()).update((beat['opening_state'], beat['ending_state']))
    for track in previous.get('prop_state_tracks', []):
        protected.setdefault(track['prop_id'], set()).add(track['initial_state'])
    return {track['prop_id']: sorted(selected.get(track['prop_id'], set()) - protected[track['prop_id']])
            for track in previous.get('prop_state_tracks', [])}


def merge_repaired_tracks(previous, candidate, tracks, shot_ids):
    """Keep unrelated prompts immutable while repairing shared state defects.

    This is for draft planning only. Full structural and semantic review still
    follows; no accepted assets or project records are modified here.
    """
    if not isinstance(tracks, list) or len(tracks) > 8:
        raise ValueError('局部修订的道具状态轨迹格式无效')
    old = {t['prop_id']: Track.model_validate(t) for t in previous.get('prop_state_tracks', [])}
    validated = [Track.model_validate(t) for t in tracks]
    ids = [t.prop_id for t in validated]
    if len(ids) != len(set(ids)):
        raise ValueError('局部修订重复返回了同一道具身份，请重试未完成规划')
    unknown = set(ids) - old.keys()
    if unknown:
        raise ValueError('局部修订返回未知道具身份：' + '、'.join(sorted(unknown))
                         + '；请使用原有道具ID重试未完成规划')
    # A local repair is a patch, not a replacement of the project catalog.
    # Omitted tracks are unchanged, never deleted or guessed by list position.
    patches = {t.prop_id: t for t in validated}
    validated = [patches.get(key, value.model_copy(deep=True)) for key, value in old.items()]
    allowed = repair_scope(previous, shot_ids)
    for track in validated:
        origin = old[track.prop_id]
        if track.initial_state != origin.initial_state:
            raise ValueError('局部修订不能改变道具初始状态')
        states = {s.id: s for s in track.states}
        if len(states) != len(track.states):
            raise ValueError('局部修订的道具状态重复')
        for state in origin.states:
            if state.id not in states or (state.id not in allowed[track.prop_id] and states[state.id] != state):
                raise ValueError('局部修订不能删改未修订镜头或初始状态依赖的道具定义')
        used = {beat[key] for shot in candidate['shots'] for beat in shot.get('prop_timeline', [])
                if beat['prop_id'] == track.prop_id for key in ('opening_state', 'ending_state')}
        # An unused newly proposed state never reaches any prompt. Discard it
        # instead of failing an otherwise coherent paid planning response.
        retained = used | {s.id for s in origin.states}
        track.states = [state for state in track.states if state.id in retained]
    updated = copy.deepcopy(candidate)
    updated['prop_state_tracks'] = [t.model_dump() for t in validated]
    compile_timeline(updated, updated.get('props', []), updated['shots'])
    return updated


def apply(shot, rows):
    updated = copy.deepcopy(shot)
    rows = normalize(rows)
    if not rows:
        return updated
    updated['continuity_states'] = rows
    notes, video = [], []
    for row in rows:
        if row['visibility'] == 'opening':
            notes.append(row['name'] + '：' + row['opening']['description'])
        else:
            notes.append(row['name'] + '开场不在可见范围内，不画入开场；依据：' + row['visibility_basis'])
        if row['visibility'] == 'offscreen':
            continue
        video.append('Opening physical state: ' + row['opening']['description_en']
                     + ' Ending physical state: ' + row['ending']['description_en'])
    updated['image_prompt'] = updated.get('image_prompt', '').split(IMAGE_MARKER, 1)[0] + IMAGE_MARKER + '\n' + '\n'.join(notes)
    plan = updated['native_dialogue']
    plan['scene'] = plan['scene'].split(VIDEO_MARKER, 1)[0] + VIDEO_MARKER + ' '.join(video)
    from .h3_dialogue import validate_dialogue_draft
    updated['native_dialogue'] = validate_dialogue_draft(plan)
    return updated
