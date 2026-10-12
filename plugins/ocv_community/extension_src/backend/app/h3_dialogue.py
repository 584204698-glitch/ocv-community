"""H3 Ref2VA native speech contract, separate from effects-only narration.

Qwen designs the reference voices; H3 generates the actual spoken performance.
The compiler follows the official six-section Ref2VA skill. English staging is
an internal agent output; user-facing names and verbatim words remain Chinese.
Reference conditioning is not evidence that the generated performance passed QA.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_serializer, model_validator
from .tts_emotion import Emotion, DEFAULT_STRENGTH


class Speaker(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    character_id: str = Field(pattern=r'^pc_[a-f0-9]{32}$')
    name: str = Field(min_length=1, max_length=80)
    picture: int | None = Field(default=None, ge=1, le=9, strict=True)
    position: str = Field(default='', max_length=240)
    appearance: str = Field(default='', max_length=600)
    voice_description: str = Field(default='', max_length=300)
    kind: Literal['character', 'narrator'] = 'character'
    on_screen: bool = True
    # Visible during the shot does not mean visible in its opening image.
    presence: Literal['opening', 'later'] = 'opening'
    voice_mode: Literal['reference', 'generated'] = 'reference'
    voice_id: str | None = Field(default=None, pattern=r'^qwen3:[a-f0-9]{32}$')

    @model_serializer(mode='wrap')
    def stable_legacy_shape(self, handler):
        value = handler(self)
        if self.voice_mode == 'reference':
            value.pop('voice_mode', None)
        # Preserve old request fingerprints for unchanged opening-only shots.
        if self.presence == 'opening':
            value.pop('presence', None)
        return value


class Turn(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    character_id: str = Field(pattern=r'^pc_[a-f0-9]{32}$')
    text: str = Field(min_length=1, max_length=100)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    delivery: str = Field(default='naturally and clearly', min_length=1, max_length=240)
    mouth_visible: bool | None = Field(default=None, strict=True)
    emotion: Emotion = 'auto'
    emotion_strength: float = Field(default=DEFAULT_STRENGTH, ge=0, le=1, allow_inf_nan=False)

    @model_serializer(mode='wrap')
    def stable_legacy_shape(self, handler):
        value = handler(self)
        if self.mouth_visible is None:value.pop('mouth_visible', None)
        if self.emotion == 'auto':value.pop('emotion', None)
        if self.emotion_strength == DEFAULT_STRENGTH:value.pop('emotion_strength', None)
        return value


class PerformanceBeat(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    action: str = Field(min_length=8, max_length=1000)
    action_zh: str = Field(min_length=2, max_length=800)


def performance_intervals(duration, turns):
    """One action slot per utterance and nonempty gap; never split spoken text."""
    intervals, previous = [], 0
    for turn in turns:
        if turn.start > previous:
            intervals.append((previous, turn.start))
        intervals.append((turn.start, turn.end))
        previous = turn.end
    if previous < duration:
        intervals.append((previous, duration))
    return intervals


class DialoguePlan(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    duration: int = Field(ge=4, le=15, strict=True)
    scene: str = Field(min_length=8, max_length=4000)
    soundscape: str = Field(default='Subtle location ambience and quiet clothing movement.', min_length=1, max_length=800)
    speakers: list[Speaker] = Field(max_length=9)
    turns: list[Turn] = Field(max_length=6)
    performance_beats: list[PerformanceBeat] = Field(default_factory=list, max_length=13)

    @model_serializer(mode='wrap')
    def stable_legacy_shape(self, handler):
        value = handler(self)
        if not self.performance_beats:
            value.pop('performance_beats', None)
        return value

    @model_validator(mode='after')
    def coherent(self, info: ValidationInfo):
        ids = [s.character_id for s in self.speakers]
        pictures = [s.picture for s in self.speakers if s.picture is not None and s.picture != 1]
        if len(set(ids)) != len(ids) or len(set(pictures)) != len(pictures):
            raise ValueError('角色与身份图必须一一对应，不能重复或按名字猜绑定')
        vocal_ids = {t.character_id for t in self.turns}
        if not vocal_ids.issubset(ids):
            raise ValueError('每条台词须明确绑定本镜说话角色')
        if len(vocal_ids) > 3:
            raise ValueError('当前 H3 节点每镜最多三个音频参考，请拆镜；不是限制整个故事的人数')
        for speaker in self.speakers:
            if speaker.picture == 1 and (speaker.kind != 'character' or speaker.presence != 'opening' or not speaker.on_screen):
                raise ValueError('核心图身份只适用于已核对的开场可见角色')
            if speaker.kind == 'character' and (speaker.picture is None or not speaker.position):
                raise ValueError('角色须绑定身份图并说明本镜位置')
            if speaker.kind == 'narrator' and (speaker.picture is not None or speaker.on_screen):
                raise ValueError('独立旁白是画外音，不绑定虚构的可见人物')
            if speaker.presence == 'later' and (speaker.kind != 'character' or not speaker.on_screen):
                raise ValueError('后入画计划仅用于本镜稍后实际可见的角色，不能用于旁白或全程画外角色')
            if speaker.character_id in vocal_ids and not speaker.voice_id and speaker.voice_mode != 'generated' and not (info.context or {}).get('unbound_draft'):
                raise ValueError('说话角色须先设计并采用固定音色')
        end = 0
        for turn in self.turns:
            if turn.start < end or turn.end <= turn.start or turn.end > self.duration:
                raise ValueError('短对话须依次发言，不能重叠或超出镜头')
            if len(re.findall(r'\w', turn.text)) > (turn.end - turn.start) * 6:
                raise ValueError('台词超过短句时长容量，请拆句或延长镜头')
            normalize_dialogue(turn.text)
            end = turn.end
        prose = [self.scene, self.soundscape, *[t.delivery for t in self.turns]]
        if self.performance_beats:
            expected = performance_intervals(self.duration, self.turns)
            spoken = {(turn.start, turn.end) for turn in self.turns}
            index = 0
            for start, end in expected:
                cursor, count = start, 0
                while index < len(self.performance_beats) and cursor < end - 1e-6:
                    beat = self.performance_beats[index]
                    if (abs(beat.start-cursor) > 1e-6 or beat.end <= cursor
                            or beat.end > end + 1e-6):
                        raise ValueError('动作节拍必须连续覆盖整镜，不能漏段、重叠或跨越台词边界')
                    beat.start = cursor
                    if abs(beat.end-end) <= 1e-6:
                        beat.end = end
                    cursor = beat.end
                    index += 1
                    count += 1
                if abs(cursor-end) > 1e-6 or ((start,end) in spoken and count != 1):
                    raise ValueError('每句台词须对应完整动作段；无对白间隙可细分连续动作，不能漏段或拆分发言')
            if index != len(self.performance_beats):
                raise ValueError('动作节拍超出镜头或重复覆盖已有时间')
            for beat in self.performance_beats:
                prose.append(beat.action)
                if not re.search(r'[\u3400-\u9fff]', beat.action_zh):
                    raise ValueError('动作节拍必须提供对应的中文说明')
        for speaker in self.speakers:
            prose.extend((speaker.position, speaker.appearance, speaker.voice_description))
        for text in [*prose, *[s.name for s in self.speakers], *[t.text for t in self.turns],
                     *[b.action_zh for b in self.performance_beats]]:
            if any(c in text for c in ('<', '>', '\n', '\r', '[', ']')) or any(ord(c) < 32 for c in text):
                raise ValueError('对话内容不能注入提示词标签或额外章节')
        if any(any(c.isalpha() and not c.isascii() for c in text) for text in prose):
            raise ValueError('H3 官方 Skill 要求场景、位置与声音描述用英文；实际台词保留中文')
        return self


def validate_dialogue_draft(value):
    """Persist a storyboard awaiting voice adoption; never use it for submission.

    Both build_prompt and resolve_voices revalidate with binding required.
    """
    return DialoguePlan.model_validate(value, context={'unbound_draft': True}).model_dump()


def normalize_dialogue(text: str) -> str:
    """Ref2VA permits punctuation normalization, never paraphrasing words."""
    text = text.translate(str.maketrans({'，': ',', '、': ',', '。': '.', '？': '?',
        '！': '!', '；': ',', ';': ',', '：': ',', ':': ',', '…': '.', '~': '', '～': '',
        '“': '', '”': '', '‘': '', '’': '', '「': '', '」': '', '『': '', '』': ''}))
    text = re.sub(r'[^\w\s,.?!\-\'％%+/=]', '', text)
    text = re.sub(r'([,.?!])\1+', r'\1', text).strip()
    if not re.search(r'\w', text):
        raise ValueError('台词必须包含实际文字')
    return text if text.endswith(('.', '?', '!')) else text.rstrip(',') + '.'


def resolve_voices(owner: int, plan: DialoguePlan, *, root=None):
    """Only immutable, saved, owner-scoped Qwen designs may be bound."""
    plan = DialoguePlan.model_validate(plan.model_dump())
    ordered = list(dict.fromkeys(turn.character_id for turn in plan.turns))
    by_id = {s.character_id: s for s in plan.speakers}
    result = []
    for identity in ordered:
        speaker = by_id[identity]
        if speaker.voice_mode == 'generated':
            continue
        index = len(result) + 1
        voice = resolve_saved_voice(owner, speaker.voice_id, root=root)
        result.append({'character_id': identity, **voice, 'speaker': f'S{ordered.index(identity) + 1}',
                       'audio_label': f'<Audio {index}>'})
    return result


def resolve_saved_voice(owner: int, voice_id: str, *, root=None):
    """Return verified bytes for one adopted voice; paths are server-only."""
    from . import voice_design
    if type(owner) is not int or owner < 1:
        raise ValueError('音色账号无效')
    if not isinstance(voice_id, str) or not re.fullmatch(r'qwen3:[a-f0-9]{32}', voice_id):
        raise ValueError('音色标识无效')
    base = Path(root) if root is not None else voice_design.ROOT
    folder = base / str(owner) / voice_id.removeprefix('qwen3:')
    audio = folder / 'design/chunk-0001.wav'
    for file in (folder / 'record.json', audio):
        if not file.resolve().is_relative_to((base / str(owner)).resolve()):
            raise ValueError('角色音频超出账号目录')
    record = json.loads((folder / 'record.json').read_text(encoding='utf-8'))
    if (record.get('status') != 'saved' or record.get('id') != voice_id.removeprefix('qwen3:')
            or not audio.is_file()):
        raise ValueError('请先试听并采用本账号的角色音色，未保存的声音不能自动绑定')
    return {'voice_id': voice_id, 'path': str(audio),
            'sha256': hashlib.sha256(audio.read_bytes()).hexdigest()}


PROMPT_REVISION = 'native-dialogue-v17-silent-mouth-performance'


def _scene_description(scene):
    # The prop catalog is story-wide design metadata, not a shot timeline.
    # In state-aware plans the core image supplies appearance, while explicit
    # per-shot boundaries and timed actions supply the allowed changes. Never
    # ask H3 to render later catalog states (e.g. baked bread while kneading).
    catalog = ' Object identity constraints: '
    boundary = ' Prop state continuity: '
    if catalog in scene and boundary in scene.split(catalog, 1)[1]:
        opening, metadata = scene.split(catalog, 1)
        _, states = metadata.split(boundary, 1)
        scene = opening + boundary + states
    # Only collapse byte-identical boundaries inserted by the prop-state writer.
    # Do not delete changed states or shorten arbitrary creative descriptions.
    marker = ' Prop state continuity: '
    if marker not in scene:
        return scene
    action, states = scene.split(marker, 1)
    def compact(match):
        opening, ending = match.group(1).strip(), match.group(2).strip()
        return ('Opening and ending physical state: ' + opening + ' '
                'Intermediate changes follow the timed actions. ' if opening == ending else
                'Opening physical state: ' + opening + ' Ending physical state: ' + ending + ' ')
    states = re.sub(r'Opening physical state: (.*?) Ending physical state: (.*?)(?= Opening physical state: |$)',
                    compact, states)
    return action + marker + states.strip()


def _visible_action(action):
    """Omit explicitly invisible, silent preparations from the render prompt.

    The approved plan retains these continuity notes. Only an off-frame event
    explicitly paired with 'Neither ... enters/appears' is eligible; observable
    sound/voices and entrances remain part of the performance.
    """
    pattern=r'\b(?:Outside the frame|Off[- ]screen)[^.!?]*[.!?]\s*Neither[^.!?]*\b(?:enters?|appears?)\b[^.!?]*[.!?]'
    audible=r'\b(?:sound|heard|hear|knock|knocks|knocking|footsteps|shout|shouts|calls|says|speaks|whispers|cries|ring|rings|crash|impact)\b|<d>'
    def keep_observable(match):
        return match.group() if re.search(audible,match.group(),re.I) else ''
    return re.sub(pattern,keep_observable,action,flags=re.I).strip()


def build_prompt(plan: DialoguePlan, *, picture_count: int, dialogue_audio_guide=False, core_identity_ids=()):
    # Revalidate even a model mutated after initial validation.
    plan = DialoguePlan.model_validate(plan.model_dump())
    if any(s.picture == 1 and s.character_id not in core_identity_ids for s in plan.speakers):
        raise ValueError('核心图身份缺少当前图片的明确核对，不能猜测角色绑定')
    if type(picture_count) is not int or not 1 <= picture_count <= 9:
        raise ValueError('参考图片数量无效')
    if {1, *[s.picture for s in plan.speakers if s.picture is not None]} != set(range(1, picture_count + 1)):
        raise ValueError('角色身份图与实际上传清单不一致，不允许未声明的额外参考图')
    ordered = list(dict.fromkeys(t.character_id for t in plan.turns))
    speakers = {s.character_id: s for s in plan.speakers}
    numbers = {identity: i for i, identity in enumerate(ordered, 1)}
    audio_numbers = {identity: i for i, identity in enumerate(
        [identity for identity in ordered if speakers[identity].voice_mode == 'reference'], 1)}
    subjects = {s.character_id: i for i, s in enumerate(
        [s for s in plan.speakers if s.kind == 'character'], 1)}
    definitions = ['<Picture 1> is the composition, setting, style and lighting reference.']
    retention = ['<Picture 1> ([Shot 1] composition anchor): fully_preserved - keep its setting, style and lighting.']
    events, staging = [], []
    for speaker in plan.speakers:
        if speaker.kind != 'character':
            continue
        label = f'<Subject {subjects[speaker.character_id]}>'
        if speaker.picture == 1:
            definitions.append(f'{label} is the explicitly confirmed opening character at {speaker.position.rstrip(".")} '
                               f'in <Picture 1>. Preserve that character\'s facial identity, hairstyle and clothing. {speaker.appearance}')
            retention.append(f'{label} (used in [Shot 1]): fully_preserved - use the confirmed character in '
                             '<Picture 1>; do not invent a separate identity or reveal occluded facial features.')
        elif speaker.presence == 'opening':
            appearance = speaker.appearance or 'Preserve this character\'s hairstyle and clothing in <Picture 1>.'
            definitions.append(f'{label} is the character whose facial identity comes from <Picture {speaker.picture}>. '
                               f'Its hairstyle and clothing come from its existing appearance in <Picture 1>. {appearance}')
            retention.append(f'{label} (used in [Shot 1]): fully_preserved - preserve facial identity and anatomy from '
                             f'<Picture {speaker.picture}>, and this shot\'s hairstyle and clothing from <Picture 1>; '
                             'the identity image does not replace the opening costume or hairstyle, and its background '
                             'and full-body presentation are not part of this role.')
        else:
            appearance = speaker.appearance or 'Preserve the referenced appearance, clothing and body proportions.'
            definitions.append(f'{label} is the character whose identity and clothing come from <Picture {speaker.picture}>. {appearance}')
            retention.append(f'{label} (used in [Shot 1]): fully_preserved - preserve identity, clothing and anatomy from <Picture {speaker.picture}>; '
                             'the image background and full-body presentation are not part of this role.')
        location = (f'initially outside the visible frame or occluded at {speaker.position}; '
                    'this character becomes visible only through the entry or reveal described in this scene'
                    if speaker.presence == 'later' else
                    f'visible at {speaker.position}' if speaker.on_screen else f'off-screen at {speaker.position}')
        staging.append(f'{label} from <Picture {speaker.picture}> is {location.rstrip(".")}.')
        if speaker.on_screen and speaker.presence != 'later':
            staging.append(f'At the opening, show only the parts of {label} visible in <Picture 1>, '
                           'with the same occlusion. Keep that visible extent until a timed action '
                           'explicitly changes the occlusion or framing'
                           + ('; speaking by itself is not a reveal action.' if plan.turns else '.'))
        if not plan.turns:
            staging.append(f'{label} keeps its mouth naturally closed. '
                           'Its jaw rests, without repeated speech-like articulation. '
                           'Mouth movement occurs only when required by an explicitly planned nonverbal action.')
    for identity in ordered:
        n, speaker = numbers[identity], speakers[identity]
        source = f'<Subject {subjects[identity]}>' if speaker.kind == 'character' else 'the off-screen narrator'
        if dialogue_audio_guide:
            definitions.append(f'S{n} is the voice for {source} in the supplied timed dialogue audio guide. Follow its speaking intervals.')
            continue
        if speaker.voice_mode == 'generated':
            definitions.append(f'S{n} is the native generated voice for {source}. {speaker.voice_description}'.rstrip())
            if speaker.kind == 'narrator':
                definitions.append(f'S{n} is heard as narration outside the depicted scene. '
                    'A character-associated vocal identity does not make the visible character speak or hear this narration.')
            continue
        audio = audio_numbers[identity]
        # The adopted recording is authoritative. A stored character description
        # may still say "adult" after the user selects a child's voice.
        definitions.append(f'<Audio {audio}> is the voice-timbre reference for {source} (S{n}). '
                           'Use the vocal identity and natural speaking register heard in this recording.')
        retention.append(f'<Audio {audio}>: reference - timbre only; do not repeat its sample words.')
    previous_end = 0
    actions = {(beat.start,beat.end):_visible_action(beat.action) for beat in plan.performance_beats}
    def timed_action(start, end):
        if (start,end) in actions:
            return actions[(start,end)]
        # Silent intervals may contain several physical actions. Keep their
        # exact order/times instead of dropping them or merging spoken turns.
        return ' '.join(f'From {beat.start:g} to {beat.end:g} seconds: {_visible_action(beat.action)}'
                        for beat in plan.performance_beats
                        if beat.start >= start and beat.end <= end)
    for turn in plan.turns:
        n = numbers[turn.character_id]
        speaker = speakers[turn.character_id]
        source = f'<Subject {subjects[turn.character_id]}>' if speaker.kind == 'character' else 'The narrator'
        off_screen = speaker.kind == 'narrator' or not speaker.on_screen
        mouth_not_visible = not off_screen and turn.mouth_visible is False
        verb = 'says in an off-screen voiceover' if speaker.kind == 'narrator' else 'speaks off-screen' if off_screen else 'says'
        if mouth_not_visible:
            # The mouth may be absent (e.g. a machine), cropped or occluded.
            # This flag does not imply that the speaker or its face is hidden.
            verb = 'speaks in its assigned voice with no visible lip movement'
        if turn.start > previous_end:
            action = timed_action(previous_end,turn.start) or 'Continue the planned actions.'
            events.append(f'From {previous_end:g} to {turn.start:g} seconds: {action} '
                          'No articulated words; no speech, narration or singing.')
        # Keep the identity anchor beside each utterance, without copying every
        # actor's appearance and blocking once per turn.
        if speaker.kind == 'character':
            anchor = f'{source} is identified by <Picture {speaker.picture}>. '
            if speaker.presence == 'later':
                anchor += 'This turn follows the planned entrance or reveal. '
            if not speaker.on_screen:
                anchor += 'This character remains outside the visible frame; do not show a face to represent this voice. '
            events.append(anchor)
        language = (' in clear Standard Mandarin Chinese (Putonghua),'
                    if re.search(r'[\u3400-\u9fff]', turn.text) else '')
        after = (" All on-screen characters' lips remain closed. This voice has no visible speaking mouth."
                 if off_screen else
                 f' The voice comes from {source} at its planned position. '
                 'Do not reveal or invent a mouth for this line; retain the speaker\'s planned visible body and face. '
                 'The other visible characters remain silent listeners.'
                 if mouth_not_visible else
                 f' {source} alone articulates the entire utterance with its own voice. '
                 'Show its lip movement only where its mouth is visible in the planned view; '
                 'preserve the planned occlusion and framing instead of uncovering a hidden face to show speech. '
                 'Never transfer its lip movement to another character.')
        for listener in plan.speakers:
            if listener.kind != 'character' or not listener.on_screen or listener.character_id == turn.character_id:
                continue
            label = f'<Subject {subjects[listener.character_id]}>'
            later = 'Whenever visible following the planned entrance, ' if listener.presence == 'later' else ''
            if speaker.kind == 'narrator':
                after += (f' {later}{label} from <Picture {listener.picture}> continues only its timed physical actions, '
                          'with relaxed closed lips and a resting jaw. The narration is outside the scene, '
                          'not a conversation heard or answered by this character. Preserve its planned eye state and posture.')
                continue
            after += (f' The listener is {label} from <Picture {listener.picture}>. '
                      f'{later}{label} listens with its mouth closed wherever visible, following only its specified timed actions; '
                      'retain its planned eye state, posture and facial occlusion.')
        action = timed_action(turn.start,turn.end)
        voice_cue = (f', using the timbre of <Audio {audio_numbers[turn.character_id]}>,'
                     if speaker.voice_mode == 'reference' else ', using its native generated voice,')
        if dialogue_audio_guide:
            voice_cue=', following its timed audio guide,'
            language=''
            verb='articulates the supplied recording'
        events.append(f'From {turn.start:g} to {turn.end:g} seconds, ' + (action + ' ' if action else '') +
            f'{source} (S{n}){voice_cue} {verb}{language} {turn.delivery}: <d>[Chinese] {normalize_dialogue(turn.text)}</d>.{after}')
        # Give the model a visible endpoint, not only a negative speech ban.
        # Do not expose a concealed mouth or stop the planned physical action.
        if not off_screen and not mouth_not_visible:
            events.append(f'As this exact line ends, {source} brings its lips together and its jaw returns to rest; '
                          'No extra words.')
        else:
            events.append(f'This vocal event ends after its final written word; the planned physical actions continue.')
        previous_end = turn.end
    if plan.turns and previous_end < plan.duration:
        action = timed_action(previous_end,plan.duration) or 'Complete the planned post-speech actions.'
        events.append(f'From {previous_end:g} seconds through the end of the {plan.duration}-second scene: '
                      + action + ' No articulated words; no speech, narration or singing.')
    sections = {
        'subject_definitions': '\n'.join(definitions),
        'summary': ('[reference generation + audio reference] A continuous scene following the composition of <Picture 1>. '
                    'Visible characters perform the planned physical actions. '
                    'Narrator lines are an off-screen narration track outside the scene; only explicitly assigned character lines are spoken by those characters.'
                    if any(speakers[t.character_id].kind == 'narrator' for t in plan.turns) else
                    '[reference generation + audio reference] A continuous scene following the composition of <Picture 1>, with the defined characters and reference-conditioned voices delivering the specified Chinese lines in order.'),
        'retention_analysis': '\n'.join(retention),
        'detailed_description': ('Maintain the visual style, lighting and color palette of <Picture 1>.\n[Shot 1] ' +
            ' '.join(staging) + '\n' + _scene_description(plan.scene) + '\n' +
            ' '.join(events) + '\nOnly the defined speakers produce words. '
            'No portraits, duplicate figures, additional dialogue, singing, subtitles or speech bubbles.'),
        'overall_soundscape': plan.soundscape,
        'non_diegetic_music': 'N/A',
    }
    if plan.turns and any(speakers[i].voice_mode == 'generated' for i in ordered):
        sections['summary'] = ('[reference generation' + (' + audio reference' if audio_numbers else '')
            + '] A continuous scene following the composition of <Picture 1>, with the defined characters delivering the specified Chinese lines in order using their assigned voices.')
    if any(speakers[t.character_id].kind == 'narrator' for t in plan.turns):
        sections['summary'] = ('[reference generation' + (' + audio reference' if audio_numbers else '')
            + '] A continuous scene following the composition of <Picture 1>. '
            'Visible characters perform the planned physical actions. '
            'Narrator lines are an off-screen narration track outside the scene; '
            'only explicitly assigned character lines are spoken by those characters.')
    if not plan.turns:
        sections['summary'] = '[reference generation] A continuous physical-action scene following the composition of <Picture 1>.'
        sections['detailed_description'] = (
            'Maintain the visual style, lighting and color palette of <Picture 1>.\n[Shot 1] '
            + _scene_description(plan.scene) + ' ' + ' '.join(staging)
            + (' ' + timed_action(0,plan.duration) if plan.performance_beats else '')
            + ' No inserted portraits or duplicate figures. '
            'Preserve the planned location ambience and physical action sounds. '
            'Nonverbal vocal sounds occur only where explicitly described in the planned actions or soundscape. '
            'No speech, narration, singing, background music, subtitles or speech bubbles.')
    # One camera contract, outside per-character/per-turn instructions. Explicit
    # planned movement remains valid; an unspecified camera must not improvise.
    camera = ('One continuous take, no cuts or shot transitions. Execute only explicitly described camera movement; '
              'otherwise keep the camera position, focal length, framing and subject scale fixed throughout. '
              'Do not add zooms, pullbacks, reframing or viewpoint changes. ')
    sections['detailed_description'] = sections['detailed_description'].replace('[Shot 1] ', '[Shot 1] '+camera, 1)
    if plan.turns:
        sections['summary'] = '[reference generation'+(' + audio reference' if audio_numbers else '')+'] One continuous shot; only the assigned speakers say the written lines.' + (
            ' Narrator lines are an off-screen narration track outside the scene.'
            if any(speakers[t.character_id].kind=='narrator' for t in plan.turns) else '')
    if dialogue_audio_guide and plan.turns:
        sections['summary']='[reference generation + audio guide] One continuous shot. The supplied soundtrack determines speech timing; animate only the assigned speaking mouths.'
        # Audio-driven production already has the exact recording and a frozen
        # schedule. Repeating every actor/occlusion/listener rule per sentence
        # bloats the scene and can compete with the actual physical action.
        # Keep binding definitions intact, each approved action exactly once,
        # and one shared mouth/occlusion contract for the whole continuous take.
        positions=[]
        for speaker in plan.speakers:
            if speaker.kind!='character':continue
            label=f'<Subject {subjects[speaker.character_id]}>'
            presence=('Initially off-screen or concealed; enters only through the planned action. '
                      if speaker.presence=='later' else
                      'Off-screen throughout. ' if not speaker.on_screen else '')
            positions.append(f'{label}: {presence}{speaker.position}')
        beats=[f'From {beat.start:g} to {beat.end:g} seconds: {_visible_action(beat.action)}'
               for beat in plan.performance_beats]
        lines=[]
        for turn in plan.turns:
            speaker=speakers[turn.character_id]
            label=f'<Subject {subjects[turn.character_id]}>' if speaker.kind=='character' else 'The off-screen narrator'
            mouth=('Keep the mouth concealed; never move a listener\'s mouth for this voice.'
                   if turn.mouth_visible is False or not speaker.on_screen or speaker.kind=='narrator'
                   else 'Animate only this mouth to the recording.')
            lines.append(f'From {turn.start:g} to {turn.end:g} seconds: {label} (S{numbers[turn.character_id]}) '
                         f'articulates the supplied recording: <d>[Chinese] {normalize_dialogue(turn.text)}</d>. {mouth}')
        sections['detailed_description']=(
            'Maintain the visual style, lighting and color palette of <Picture 1>.\n[Shot 1] '+camera
            +_scene_description(plan.scene)+'\n'+'\n'.join(positions)+'\nActions:\n'+'\n'.join(beats)
            +'\nSpeech timing:\n'+'\n'.join(lines)
            +'\nThe supplied timed audio guide is the actual dialogue, not a voice-cloning sample. '
            'Follow its words, pauses and sounds; its delivery does not override the approved facial expression. '
            'Preserve each actor in its existing body and position; '
            'identity images do not add portraits or other people. Preserve opening occlusion until an approved '
            'action changes it; speech itself never reveals a concealed mouth. During each utterance only its '
            'assigned visible mouth moves, all listeners keep their mouths closed. All mouths rest after their '
            'recording ends and during guide silence. Explicitly planned nonverbal mouth actions still occur '
            'at their assigned times without speech-like articulation. '
            'No additional dialogue, actors, cuts, subtitles or speech bubbles.')
    sections['detailed_description'] += (
        '\nIdentity reference images bind facial identity; their smiles or other expressions do not override '
        'the approved performance. Carry any approved sustained expression through speech and the final held '
        'frames until an explicitly planned emotional change.')
    return '\n\n'.join(key + ':\n' + value for key, value in sections.items())


