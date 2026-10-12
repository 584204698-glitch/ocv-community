"""CPU-only native dialogue transcription and timing, never speaker verification."""
from __future__ import annotations

from functools import lru_cache
from difflib import SequenceMatcher
import copy
import hashlib
import json
import math
from numbers import Real
import os
import re
from pathlib import Path
import threading

from .h3_dialogue import DialoguePlan

REVISION = 'native-dialogue-asr-v4-frozen-name-differences'
_LOCK = threading.Lock()


def orthographic_key(value):
    return ''.join(c for c in str(value) if c.isalnum()).casefold()


def text_key(value):
    # Mandarin ASR cannot distinguish written third-person pronoun gender or
    # animacy from the sound "ta1". This narrow equivalence is not fuzzy matching;
    # do not fold arbitrary homophones, missing syllables, tones or extra speech.
    return orthographic_key(value).translate(str.maketrans({'她':'他','它':'他','牠':'他','祂':'他'}))


# pypinyin includes historical readings for these modern single-pronunciation
# characters (播: bo3; 波: bei1/bi4; 路: luo4). Correct only these entries;
# never select the first reading of a genuinely polyphonic character.
_MODERN_SINGLE_READINGS = {'播': 'bo1', '波': 'bo1', '路': 'lu4'}


def _frozen_name_match(expected, actual, names):
    """Only resolve ASR spelling of a full, unambiguously pronounced cast name.

    A name's alternative ASR spelling uses its ordinary phrase reading. The
    adopted name itself must have a single modern reading for every character;
    ambiguous adopted names require listening rather than a guessed reading.
    All text outside these exact, same-length spans retains the strict rule.
    """
    if not names or len(expected) != len(actual):
        return None
    try:
        from pypinyin import pinyin, Style
    except ImportError:
        return None
    corrected=list(actual)
    changed=False
    for name in sorted({orthographic_key(n) for n in names}, key=len, reverse=True):
        if len(name)<2 or not all('\u3400'<=c<='\u9fff' for c in name):continue
        readings=[]
        for character in name:
            options=([_MODERN_SINGLE_READINGS[character]] if character in _MODERN_SINGLE_READINGS else
                     pinyin(character,style=Style.TONE3,heteronym=True,neutral_tone_with_five=True,strict=True)[0])
            if len(set(options))!=1 or not re.fullmatch(r'[a-zv]+[1-5]',options[0]):break
            readings.append(options[0])
        if len(readings)!=len(name):continue
        for match in re.finditer(re.escape(name),expected):
            candidate=actual[match.start():match.end()]
            if candidate==name or not all('\u3400'<=c<='\u9fff' for c in candidate):continue
            heard=pinyin(candidate,style=Style.TONE3,heteronym=False,neutral_tone_with_five=True,strict=True)
            if heard!=[[r] for r in readings]:continue
            corrected[match.start():match.end()]=name
            changed=True
    if changed and spoken_match_basis(expected,''.join(corrected)) is not None:
        return 'frozen_character_name_readings'
    return None


def spoken_match_basis(expected, actual, *, known_names=()):
    """Conservative spoken equivalence, not text correction or fuzzy matching."""
    expected, actual = orthographic_key(expected), orthographic_key(actual)
    if expected == actual:
        return 'normalized_text'
    expected, actual = text_key(expected), text_key(actual)
    if expected == actual:
        return 'equivalent_spoken_pronouns'
    if len(expected) != len(actual):
        return None
    differing = [(a, b) for a, b in zip(expected, actual) if a != b]
    if any(not ('\u3400' <= a <= '\u9fff' and '\u3400' <= b <= '\u9fff') for a, b in differing):
        return None
    try:
        from pypinyin import pinyin, Style
    except ImportError:
        return None

    def single_reading(character):
        if character in _MODERN_SINGLE_READINGS:
            return _MODERN_SINGLE_READINGS[character]
        rows = pinyin(character, style=Style.TONE3, heteronym=True,
                      neutral_tone_with_five=True, strict=True)
        if len(rows) != 1 or len(set(rows[0])) != 1:
            return None
        syllable = rows[0][0]
        return syllable if re.fullmatch(r'[a-zv]+[1-5]', syllable) else None

    for a, b in differing:
        reading = single_reading(a)
        if reading is None or reading != single_reading(b):
            return (_frozen_name_match(expected,actual,known_names)
                    or _frozen_name_match(actual,expected,known_names))
    return 'equivalent_single_reading_toned_syllables'


def word_differences(expected, actual):
    """Diagnostics only: never use edit distance to relax the acceptance gate.

    Offsets refer to normalized concatenated text, not audio time or inferred
    speakers. ASR mistakes remain possible even when the two strings differ.
    """
    return [dict(kind=kind, expected=expected[a:b], recognized=actual[c:d],
                 expected_start=a, expected_end=b, recognized_start=c, recognized_end=d)
            for kind,a,b,c,d in SequenceMatcher(None,expected,actual,autojunk=False).get_opcodes()
            if kind != 'equal']


@lru_cache(maxsize=1)
def _model(source):
    from faster_whisper import WhisperModel
    return WhisperModel(source, device='cpu', compute_type='int8', cpu_threads=4,
                        num_workers=1, local_files_only=True)


def _asr_number(value):
    # NumPy scalars from Whisper alignment are real numbers, but not exact
    # Python float types. Keep malformed values intact for strict validation.
    return float(value) if isinstance(value, Real) and not isinstance(value, bool) else value


def transcribe(clip, source):
    # No target text prompt: do not prime recognition into reproducing our script.
    with _LOCK:
        segments, info = _model(source).transcribe(str(clip), language='zh', beam_size=5,
            vad_filter=True, condition_on_previous_text=False, word_timestamps=True)
        return {'language': info.language, 'segments': [
            {'text': s.text, 'start': _asr_number(s.start), 'end': _asr_number(s.end),
             'words': [{'text': w.word, 'start': _asr_number(w.start), 'end': _asr_number(w.end),
                        'probability': _asr_number(w.probability)}
                       for w in s.words or []]} for s in segments]}


def compare_transcript(plan, transcript, duration):
    """Strict words or unambiguous toned equivalents; times come from ASR.

    Assignment of the expected character ID is subtitle attribution only. ASR
    does not prove which on-screen character spoke or which voice was used.
    """
    plan = DialoguePlan.model_validate(plan)
    if not isinstance(transcript, dict) or not isinstance(transcript.get('segments'), list):
        raise ValueError('原声识别未返回有效的分段')
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('原声片段时长无效')
    words, heard = [], []
    for segment in transcript['segments']:
        if not isinstance(segment, dict) or not isinstance(segment.get('text'), str):
            raise ValueError('原声识别分段无效')
        heard.append(segment['text'])
        for word in segment.get('words') or []:
            if (not isinstance(word, dict) or not isinstance(word.get('text'), str)
                    or any(type(word.get(k)) not in (int, float) for k in ('start', 'end'))):
                raise ValueError('原声识别逐词时间戳无效')
            start, end = float(word['start']), float(word['end'])
            if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end <= start or end > duration + .08:
                raise ValueError('原声识别时间戳超出片段')
            key = text_key(word['text'])
            if key:
                words.append({'key': key, 'start': start, 'end': min(end, duration)})
    expected = ''.join(text_key(turn.text) for turn in plan.turns)
    actual = text_key(''.join(heard))
    expected_spelling = orthographic_key(''.join(turn.text for turn in plan.turns))
    actual_spelling = orthographic_key(''.join(heard))
    known_names=tuple(s.name for s in plan.speakers if s.kind=='character')
    matching_basis = spoken_match_basis(expected_spelling, actual_spelling,known_names=known_names)
    report = {'expected': [t.text for t in plan.turns], 'recognized': ''.join(heard),
              'text_matches': matching_basis is not None, 'turns': [], 'issues': [],
              'speaker_verified': False, 'timbre_verified': False, 'quality_accepted': False}
    report['orthographic_text_matches'] = expected_spelling == actual_spelling
    report['matching_basis'] = matching_basis or 'mismatch'
    if not report['orthographic_text_matches']:
        report['word_differences'] = word_differences(expected_spelling, actual_spelling)
    if matching_basis == 'equivalent_single_reading_toned_syllables':
        report['matching_note'] = '仅单音字逐字同音同调；保留字形差异，不证明字形、说话者、声线或口型正确。'
    if matching_basis == 'frozen_character_name_readings':
        report['matching_note'] = '冻结角色全名的常规普通话读音一致；保留识别字形差异供试听，不证明姓名字形、说话者、声线或口型正确。'
    if matching_basis is None:
        notes = []
        for difference in report['word_differences'][:6]:
            a,b=difference['expected'],difference['recognized']
            notes.append(f'“{a}”识别为“{b}”' if a and b else f'未识别到“{a}”' if a else f'多识别出“{b}”')
        report['issues'].append({'code': 'speech_mismatch',
            'message': '识别文字与计划台词不同：'+'；'.join(notes)+
                       '。可能有错漏字、多话或识别误差；请试听本镜。'})
        return report
    word_text = ''.join(w['key'] for w in words)
    if (spoken_match_basis(expected,word_text,known_names=known_names) is None
            or spoken_match_basis(actual,word_text,known_names=known_names) is None):
        report['issues'].append({'code': 'alignment_missing', 'message': '文字相符但缺少可靠的逐词时间戳。'})
        return report
    if any(current['start'] < previous['end'] - .001 for previous, current in zip(words, words[1:])):
        report['issues'].append({'code': 'alignment_overlap', 'message': '识别逐词时间重叠或顺序错误，请核对发言时间。'})
        return report
    offset = 0
    spans = []
    for word in words:
        spans.append((offset, offset + len(word['key']), word))
        offset += len(word['key'])
    offset, last_end = 0, 0
    for turn in plan.turns:
        limit = offset + len(text_key(turn.text))
        matching = [word for begin, end, word in spans if begin < limit and end > offset]
        start, end = matching[0]['start'], matching[-1]['end']
        if start < last_end - .001:
            report['issues'].append({'code': 'alignment_overlap', 'message': '相邻角色台词的识别时间重叠，请核对发言顺序。'})
            report['turns'] = []
            return report
        report['turns'].append({'character_id': turn.character_id, 'text': turn.text, 'start': start, 'end': end})
        offset, last_end = limit, end
    return report


def screen_dialogue(path, shot, *, recognize=None):
    from .video_export import _source
    path = Path(path).resolve()
    clip = _source(path, shot)
    frozen = shot.get('video_request') or {}
    plan = (frozen.get('dialogue') or {}).get('plan')
    return _screen_media(path, clip, plan, recognize=recognize)


def _screen_media(path, clip, plan, *, recognize=None, extra_evidence=None):
    from .pipeline import probe_media_duration
    DialoguePlan.model_validate(plan)
    source = os.environ.get('OCV_DIALOGUE_ASR_MODEL') or os.environ.get('ASR_MODEL') or 'large-v3-turbo'
    evidence = {'revision': REVISION, 'clip_sha256': hashlib.sha256(clip.read_bytes()).hexdigest(),
                'plan': plan, 'model': source}
    if extra_evidence is not None:
        evidence['final_audio'] = extra_evidence
    fingerprint = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    target = path / 'assets/dialogue_reviews' / (fingerprint + '.json')
    if target.is_file():
        saved = json.loads(target.read_text(encoding='utf8'))
        if saved.get('fingerprint') != fingerprint or saved.get('evidence') != evidence:
            raise ValueError('对白检查缓存与当前片段不一致')
        # Re-evaluate cached evidence instead of trusting a persisted pass flag.
        saved['review'] = compare_transcript(plan, saved['transcript'], probe_media_duration(clip))
        return saved
    transcript = (recognize or transcribe)(clip, source)
    report = compare_transcript(plan, transcript, probe_media_duration(clip))
    saved = {'fingerprint': fingerprint, 'evidence': evidence, 'transcript': transcript, 'review': report}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(target)
    return saved


def _external_speech_basis(path, frozen):
    """Validate immutable full speech, including narration omitted from H3."""
    from .video_external_dialogue import verified
    from .video_external_narration import digest
    dialogue = frozen.get('dialogue') or {}
    basis = dialogue.get('basis') or {}
    data = frozen.get('external_dialogue')
    if (not isinstance(data, dict) or not isinstance(basis.get('plan'), dict)
            or basis.get('dialogue_route') != 'external_tts'
            or basis.get('external_dialogue') != data
            or dialogue.get('fingerprint') != digest(basis)):
        raise ValueError('最终配音检查缺少一致的冻结台词与音频依据，不能改查原H3音轨')
    plan = DialoguePlan.model_validate(basis['plan']).model_dump()
    clips = data.get('clips')
    if not isinstance(clips, list) or len(clips) != len(plan['turns']):
        raise ValueError('冻结配音没有完整覆盖本镜角色及旁白台词')
    speakers = {s['character_id']: s for s in plan['speakers']}
    voices = {v['character_id']: v for v in basis.get('voices', [])}
    for turn, clip in zip(plan['turns'], clips):
        speaker = speakers[turn['character_id']]
        voice = voices.get(turn['character_id']) or {}
        if (not isinstance(clip, dict)
                or any(clip.get(key) != turn[key] for key in ('character_id', 'text'))
                or clip.get('kind') != speaker.get('kind', 'character')
                or not speaker.get('voice_id') or clip.get('voice_id') != speaker['voice_id']
                or not voice.get('sha256') or clip.get('voice_sha256') != voice['sha256']):
            raise ValueError('冻结配音的台词、顺序、角色或音色与采用计划不一致')
        verified(path, clip)
    return plan, data


def _check_external_windows(clips, duration):
    previous = 0.0
    for clip in clips:
        values = [clip.get(key) for key in ('start', 'end', 'duration')]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise ValueError('最终配音时间轴无效')
        start, end, length = values
        if (start < 0 or start < previous - .002 or length <= 0
                or end <= start or end > duration + .002
                or abs(end - start - length) > .002):
            raise ValueError('最终配音必须完整且依次位于本镜内，不能重叠或截断台词')
        previous = end


def screen_external_dialogue(path, shot, *, volume, recognize=None):
    """Screen the muxed TTS audio; the H3 guide intentionally lacks narration."""
    from .pipeline import probe_media_duration
    from .video_export import _source, _asset
    from .video_external_dialogue import preview
    from .video_manual_dub import effective_clips
    path = Path(path).resolve()
    snapshot = copy.deepcopy(shot)
    frozen = snapshot.get('video_request') or {}
    plan, data = _external_speech_basis(path, frozen)
    source = _source(path, snapshot)
    source_duration = probe_media_duration(source)
    _check_external_windows(effective_clips(snapshot, source, data, source_duration), source_duration)
    # preview may update alignment on this private copy, never the project.
    clip = _asset(path, preview(path, snapshot, volume=volume))
    duration = probe_media_duration(clip)
    if not math.isfinite(duration) or duration <= 0 or abs(duration - source_duration) > .1:
        raise ValueError('最终配音片段时长与本镜不一致')
    effective = effective_clips(snapshot, source, data, duration)
    _check_external_windows(effective, duration)
    # The full original plan supplies text/order; ASR supplies actual times.
    # Replacing its turns with retimed clips would invalidate performance beats.
    return _screen_media(path, clip, plan, recognize=recognize, extra_evidence={
        'route': 'external_tts_final', 'revision': 'external-final-speech-v1',
        'dialogue_fingerprint': frozen['dialogue']['fingerprint'],
        'external_key': data['key'], 'frozen_clips': data['clips'],
        'effective_clips': effective, 'narrator_volume': volume,
    })


class NativeSpeechReviewRequired(ValueError):
    def __init__(self, message, shot_id, report):
        super().__init__(message)
        self.shot_id, self.report = shot_id, report


def check_native_speech(path, record, *, progress=None):
    from .video_external_dialogue import enabled
    from .video_export import _narration_volume
    external = enabled(record)
    reports = []
    for index, shot in enumerate(record['shots'], 1):
        if not external and (shot.get('video_request') or {}).get('external_dialogue'):
            raise ValueError('当前声音路线与本镜冻结配音请求不一致，请核对本镜')
        if progress:
            label = '最终TTS对白及旁白' if external else '原生对白'
            progress(f'检查第{index}镜{label}；语音识别用于筛查，不代表声线与口型已验收。')
        report = (screen_external_dialogue(path, shot, volume=_narration_volume(record)) if external
                  else screen_dialogue(path, shot))
        reports.append(report)
        if report['review']['issues']:
            label = f"镜头 {shot['id']}" if shot.get('id') else f'第{index}镜'
            raise NativeSpeechReviewRequired(f'自动成片已暂停：{label}对白需要核对。'
                             + report['review']['issues'][0]['message']
                             + '识别内容：' + report['review']['recognized'][:400], shot.get('id'), report)
    return reports
