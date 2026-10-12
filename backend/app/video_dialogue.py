"""Project-owned voice bindings and automatic per-shot H3 reference selection.

Names never select an image or a voice. Changing a binding does not rewrite old
requests; their immutable media remains reviewable and must be regenerated
explicitly before it can count as the current dialogue performance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .h3_dialogue import DialoguePlan, build_prompt, resolve_saved_voice, resolve_voices
from .video_project_cast import _saved_characters, project_cast_view


class VoiceBindingMutation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0, strict=True)
    character_id: str = Field(pattern=r'^pc_[a-f0-9]{32}$')
    # Explicit null unbinds one character. It never removes the saved voice.
    voice_mode: Literal['reference', 'generated'] = 'reference'
    voice_id: str | None = Field(default=None, pattern=r'^qwen3:[a-f0-9]{32}$')


def native_mode(record):
    return (record.get('creation_parameters') or {}).get('speech_mode') in {'dialogue', 'mixed'}


def _voice_characters(owner, record):
    rows = _saved_characters(owner, record)
    narrator = record.get('native_narrator')
    if narrator is not None:
        if (not isinstance(narrator, dict) or not re.fullmatch(r'pc_[a-f0-9]{32}', str(narrator.get('id', '')))
                or any(row['id'] == narrator['id'] for row in rows)):
            raise ValueError('旁白身份记录无效或与可见角色重复')
        rows.append({'id': narrator['id'], 'name': '旁白', 'kind': 'narrator'})
    return rows


def _bindings(owner, record):
    # Validate account/project scope even when there are no voice bindings yet.
    _saved_characters(owner, record)
    saved = record.get('project_voices')
    if saved is None:
        return {}
    if (not isinstance(saved, dict) or type(saved.get('version')) is not int or saved.get('version') != 1
            or saved.get('owner_id') != owner or saved.get('project_id') != record['id']
            or not isinstance(saved.get('characters'), dict)):
        raise ValueError('角色音色不属于当前账号或项目，请重新绑定')
    result = copy.deepcopy(saved['characters'])
    for identity, binding in result.items():
        if not isinstance(binding, dict):
            raise ValueError('角色音色绑定已损坏，请重新绑定')
        VoiceBindingMutation(revision=0, character_id=identity, voice_id=binding.get('voice_id'), voice_mode=binding.get('voice_mode', 'reference'))
        if binding.get('voice_mode') == 'generated':
            if set(binding) != {'voice_mode'}:
                raise ValueError('???????????????')
            continue
        if not binding.get('voice_id') or not isinstance(binding.get('sha256'), str) or len(binding['sha256']) != 64:
            raise ValueError('角色音色绑定已损坏，请重新绑定')
    return result


def bind_voice(owner, record, character_id, voice_id, voice_mode='reference'):
    VoiceBindingMutation(revision=0, character_id=character_id, voice_id=voice_id, voice_mode=voice_mode)
    cast = {row['id'] for row in _voice_characters(owner, record)}
    if character_id not in cast:
        raise ValueError('请先保存本项目角色，不能按同名角色或其他项目的角色绑定音色')
    from .video_external_dialogue import enabled as exact_enabled
    if voice_mode=='generated' and exact_enabled(record):
        raise ValueError('准确配音路线需要采用固定音色；H3自行生成声音仅用于原生对白路线')
    bindings = _bindings(owner, record)
    if voice_mode == 'generated':
        bindings[character_id] = {'voice_mode': 'generated'}
    elif voice_id is None:
        bindings.pop(character_id, None)
    else:
        voice = resolve_saved_voice(owner, voice_id)
        from .h3_voice_reference import inspect_voice_reference
        inspect_voice_reference(voice['path'], voice['sha256'])
        bindings[character_id] = {key: voice[key] for key in ('voice_id', 'sha256')}
    return {'version': 1, 'owner_id': owner, 'project_id': record['id'], 'characters': bindings}


def voice_view(owner, record):
    bindings = _bindings(owner, record)
    result = []
    for character in _voice_characters(owner, record):
        binding = bindings.get(character['id'])
        row = {'character_id': character['id'], 'name': character['name'], 'kind': character.get('kind', 'character'),
               'voice_id': binding.get('voice_id') if binding else None,
               'voice_mode': binding.get('voice_mode', 'reference') if binding else 'reference', 'status': 'unbound', 'message': ''}
        if binding and binding.get('voice_mode') == 'generated':
            row['status'] = 'generated'
        elif binding:
            try:
                actual = resolve_saved_voice(owner, binding['voice_id'])
                if actual['sha256'] != binding['sha256']:
                    raise ValueError('参考声音已变化，请重新试听并绑定')
                from .h3_voice_reference import inspect_voice_reference
                inspect_voice_reference(actual['path'], actual['sha256'])
                row['status'] = 'current'
                row['audio_url'] = '/api/voice-design/' + binding['voice_id'].removeprefix('qwen3:') + '/audio'
            except (OSError, ValueError):
                row.update(status='stale', message='参考声音不可用，请重新试听并绑定')
        result.append(row)
    return {'project_id': record['id'], 'revision': record['revision'], 'characters': result,
            'project_status': record.get('status'),
            'stale_shot_ids': [shot['id'] for shot in record.get('shots', []) if shot.get('dialogue_stale')]}


def _reuse_external_image_binding(path, shot, plan, basis, engine):
    """A picture-only rebind keeps verified, immutable speech and guide bytes."""
    for entry in [shot, *reversed(shot.get('video_history') or [])]:
        rebound = _rebind_external_request(path,entry.get('video_request') or {},plan,basis,engine)
        if rebound is not None:return rebound
    return None


def _rebind_external_request(path, frozen, plan, basis, engine):
    from . import video_external_dialogue as exact
    previous = (frozen.get('dialogue') or {}).get('basis') or {}
    external = frozen.get('external_dialogue')
    def without_pictures(value):
        result = copy.deepcopy(value)
        if not isinstance(result, dict):return None
        for speaker in result.get('speakers', []):speaker.pop('picture', None)
        return result
    if (not external or previous.get('external_dialogue') != external
            or previous.get('dialogue_route') != 'external_tts'
            or previous.get('dialogue_tts_engine') != engine
            or any(previous.get(key) != basis.get(key) for key in ('voices','identities','reference_audio_policy'))
            or without_pictures(previous.get('plan')) != without_pictures(plan)):
        return None
    clips = external.get('clips') or []
    if len(clips) != len(plan['turns']):return None
    for clip, turn in zip(clips, plan['turns']):
        if any(clip.get(key) != turn.get(key) for key in ('character_id','text')):return None
        exact.verified(path, clip)
    exact.verified(path, external['guide'])
    visual, timed = exact.retime(plan, dict(enumerate(clips)))
    if (timed != clips or without_pictures(visual) != without_pictures(external.get('plan'))
            or external['guide']['duration'] != visual['duration']):
        return None
    return {**copy.deepcopy(external), 'plan':visual,
            'key':exact.digest(dict(version=exact.REVISION,clips=timed,plan=visual))}


def prepare_dialogue(owner, path, record, shot, selection, images, *, prepare_narration=False):
    """Compile current storyboard + exact images + project voices, with no guesswork.

    Picture and voice fields in a planning draft are replaced with the actual
    selected resources. Only referenced cast members are passed through. Saved
    character IDs survive reordering; H3's per-clip S/Audio IDs follow speech.
    """
    from .video_inputs import asset_rows
    if selection['input_mode'] != 'composition_reference':
        raise ValueError('原生角色对白需要 H3 多参模式，首尾帧模式没有音色参考输入')
    draft = shot.get('native_dialogue')
    if not isinstance(draft, dict):
        raise ValueError('本镜缺少对话计划')
    from .video_native_staging import assert_staging_current
    assert_staging_current(shot)
    value = copy.deepcopy(draft)
    bindings = _bindings(owner, record)
    cast = {row['id']: row for row in project_cast_view(owner, path, record, asset_rows(record))['characters']}
    narrators = {row['id']: row for row in _voice_characters(owner, record) if row.get('kind') == 'narrator'}
    positions = {row['id']: i for i, row in enumerate(images, 1) if row['role'] == 'identity_reference'}
    core_identity_ids = set()
    coverage = None
    vocal_ids = {turn.get('character_id') for turn in value.get('turns', [])}
    for speaker in value.get('speakers', []):
        # Planning output cannot opt a character into generated speech.
        speaker['voice_mode'] = 'reference'
        identity = speaker.get('character_id')
        if speaker.get('kind', 'character') == 'narrator':
            if identity not in narrators:
                raise ValueError('本镜旁白不属于当前项目')
            speaker.update(name='旁白', picture=None, on_screen=False, voice_id=None)
        else:
            actor = cast.get(identity)
            if not actor or actor['status'] != 'current':
                raise ValueError('本镜对话引用的项目角色缺失或身份已变化，请核对角色资产')
            picture = positions.get(actor['reference_id'])
            if picture is None:
                from .video_character_coverage import character_coverage
                if coverage is None:
                    coverage = character_coverage(owner,path,record,shot,selection,asset_rows(record))
                confirmed = next((row for row in coverage['characters']
                                  if row.get('project_character_id') == identity
                                  and row.get('core_identity_confirmed') is True
                                  and row.get('covered') and row.get('via') == 'core'
                                  and row.get('presence') == 'opening'), None)
                if (not confirmed or 'video_input_selection' not in shot
                        or actor['reference_id'] in selection.get('reference_ids', [])
                        or speaker.get('presence', 'opening') != 'opening' or not speaker.get('on_screen', True)
                        or any(row.get('project_character_id') == identity and row.get('presence') == 'later'
                               for row in (shot.get('cast_plan') or {}).get('characters', []))
                        or not images or images[0]['role'] != 'composition'
                        or Path(images[0]['path']).resolve() != (Path(path)/shot['image']).resolve()):
                    raise ValueError('本镜缺少说话角色的身份参考图；请采用独立角色图，或明确核对当前核心图中的开场角色身份')
                picture = 1
                core_identity_ids.add(identity)
            speaker.update(name=actor['name'], picture=picture, voice_id=None)
        if identity in vocal_ids:
            binding = bindings.get(identity)
            if not binding:
                raise ValueError('本镜说话角色尚未绑定已采用的 Qwen 音色')
            speaker['voice_mode'] = binding.get('voice_mode', 'reference')
            speaker['voice_id'] = binding.get('voice_id')
    plan = DialoguePlan.model_validate(value)
    if plan.duration != shot.get('generation_duration'):
        raise ValueError('对话计划时长与当前镜头不一致，请重新规划')
    voices = resolve_voices(owner, plan)
    for voice in voices:
        if voice['sha256'] != bindings[voice['character_id']]['sha256']:
            raise ValueError('角色参考音色已变化，请重新试听并绑定')
    from .h3_voice_reference import POLICY
    basis = {'plan': plan.model_dump(), 'voices': [{k: v for k, v in voice.items() if k != 'path'} for voice in voices],
             'identities': {s.character_id: cast[s.character_id]['identity_sha256'] if s.kind == 'character'
                            else 'off-screen-narrator' for s in plan.speakers}}
    if voices:
        basis['reference_audio_policy'] = POLICY
    image_basis = []
    for image in images:
        file = Path(image['path']).resolve()
        if not file.is_relative_to(Path(path).resolve()) or not file.is_file():
            raise ValueError('对白参考图片不属于当前项目或已缺失')
        image_basis.append({'id':image['id'], 'sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
    basis['images'] = image_basis
    if core_identity_ids:
        review = shot['video_character_review']
        basis['core_identity_confirmations'] = dict(
            character_ids=sorted(core_identity_ids), core_image=copy.deepcopy(review['core_image']),
            source_fingerprint=review['source_fingerprint'])
    from .video_external_narration import enabled, prepare
    from . import video_external_dialogue as exact
    if exact.enabled(record):
        external=_reuse_external_image_binding(path,shot,plan.model_dump(),basis,exact.engine(record))
        if external is None:
            external=exact.prepare(owner,path,record,plan.model_dump(),voices,allow_synthesis=prepare_narration)
        basis['dialogue_route']='external_tts'
        basis['dialogue_tts_engine']=exact.engine(record)
        basis['external_dialogue']=external
        fingerprint=hashlib.sha256(json.dumps(basis,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        render_plan=DialoguePlan.model_validate(external['plan']) if external else plan
        return dict(prompt=build_prompt(render_plan,picture_count=len(images),dialogue_audio_guide=True,
                                      core_identity_ids=core_identity_ids),plan=plan.model_dump(),
            render_plan=render_plan.model_dump(),external_dialogue=external,external_narration=None,
            voices=[],basis=basis,fingerprint=fingerprint,version=3)
    external = prepare(owner,path,record,plan.model_dump(),voices,allow_synthesis=prepare_narration)
    has_narrator=any(s.kind=='narrator' and any(t.character_id==s.character_id for t in plan.turns) for s in plan.speakers)
    if enabled(record) and has_narrator:
        basis['narration_route'] = 'external_tts'
        basis['external_narration'] = external
    fingerprint = hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    render_plan=DialoguePlan.model_validate(external['plan']) if external else plan
    # Audio numbers are recomputed from the reduced actual H3 speaker order.
    render_voices=resolve_voices(owner,render_plan) if external else voices
    return {'prompt': build_prompt(render_plan, picture_count=len(images),core_identity_ids=core_identity_ids), 'plan': plan.model_dump(),
            'render_plan':render_plan.model_dump(), 'external_narration':external,
            'voices': render_voices, 'basis': basis, 'fingerprint': fingerprint, 'version': 2}


def _legacy_voice_policy_only(frozen, current):
    """An unchanged creative request may still be viewed after excerpt upgrades.

    Only the initial, absent excerpt policy can migrate for observation. Any
    changed words, timing, identities, image bytes or saved voice stays stale.
    Automatic validation always requires the current fingerprint.
    """
    from .h3_voice_reference import POLICY
    old, new = frozen.get('basis'), current.get('basis')
    if not isinstance(old, dict) or not isinstance(new, dict):
        return False
    if 'reference_audio_policy' in old or new.get('reference_audio_policy') != POLICY:
        return False
    if not old.get('voices'):
        return False
    digest = hashlib.sha256(json.dumps(old, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return frozen.get('fingerprint') == digest and old == {
        k: v for k, v in new.items() if k != 'reference_audio_policy'}


def silent_external_engine_equivalent(owner, path, record, shot, *, current=None):
    """Ignore only an engine change that cannot affect an exact silent request."""
    frozen = shot.get('video_request') or {}
    saved = frozen.get('dialogue') or {}
    old = saved.get('basis')
    if (shot.get('kind') != 'video' or shot.get('video_status') != 'completed'
            or (frozen.get('audio_policy') or {}).get('mode') != 'external_dialogue'
            or not isinstance(old, dict) or old.get('dialogue_route') != 'external_tts'
            or (old.get('plan') or {}).get('turns') != []
            or (shot.get('native_dialogue') or {}).get('turns') != []
            or old.get('voices') != [] or frozen.get('external_narration')
            or any(s.get('kind') == 'narrator' for s in (old.get('plan') or {}).get('speakers', []))):
        return False
    external = old.get('external_dialogue')
    if (not isinstance(external, dict) or external != frozen.get('external_dialogue')
            or external.get('clips') != [] or (external.get('plan') or {}).get('turns') != []):
        return False
    digest = lambda basis: hashlib.sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if saved.get('fingerprint') != digest(old):
        return False
    try:
        if current is None:
            from .video_inputs import resolve_video_inputs
            selection, images = resolve_video_inputs(path, record, shot)
            current = prepare_dialogue(owner, path, record, shot, selection, images)
        new = current.get('basis')
        if (not isinstance(new, dict) or current.get('fingerprint') != digest(new)
                or {old.get('dialogue_tts_engine'), new.get('dialogue_tts_engine')} != {'qwen3', 'indextts25'}
                or {k: v for k, v in old.items() if k != 'dialogue_tts_engine'}
                   != {k: v for k, v in new.items() if k != 'dialogue_tts_engine'}):
            return False
        from .video_external_dialogue import verified
        verified(path, external['guide'])
    except (ValueError, OSError, KeyError, TypeError):
        return False
    return True


def assert_native_export_current(owner, path, record, *, observation_only=False):
    """Never label previously generated words/voices as the current revision."""
    from .video_inputs import resolve_video_inputs
    if not native_mode(record):
        return
    if not record.get('shots'):
        raise ValueError('原生对话项目没有镜头')
    legacy=[]
    for index, shot in enumerate(record['shots'], 1):
        if shot.get('kind') != 'video' or shot.get('video_status') != 'completed':
            raise ValueError(f'第{index}镜原生对话视频尚未完成')
        frozen = shot.get('video_request') or {}
        from .video_external_dialogue import enabled as exact_enabled
        expected_mode='external_dialogue' if exact_enabled(record) else 'native_dialogue'
        if (frozen.get('audio_policy') or {}).get('mode') != expected_mode:
            raise ValueError(f'第{index}镜尚未按原生对话流程生成')
        selection, images = resolve_video_inputs(path, record, shot)
        current = prepare_dialogue(owner, path, record, shot, selection, images)
        if (frozen.get('dialogue') or {}).get('fingerprint') != current['fingerprint']:
            if silent_external_engine_equivalent(owner, path, record, shot, current=current):
                continue
            if observation_only:
                from .video_dynamic_review import reviewed_nonvisual_original_plan
                prior=reviewed_nonvisual_original_plan(path,record,shot)
                if prior is not None:
                    retained=copy.deepcopy(shot);retained['native_dialogue']=prior
                    original_request=prepare_dialogue(owner,path,record,retained,selection,images)
                    if (frozen.get('dialogue') or {}).get('fingerprint')==original_request['fingerprint']:
                        continue
            if observation_only and _legacy_voice_policy_only(frozen.get('dialogue') or {}, current):
                legacy.append(shot['id'])
                continue
            raise ValueError(f'第{index}镜台词、角色或音色已修改，请局部重做本镜后合成；旧片保留。')
    return legacy
