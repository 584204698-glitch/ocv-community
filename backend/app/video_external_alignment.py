"""Align an existing speaking performance to exact TTS; no GPU or new script."""
import hashlib
import json
import os
from pathlib import Path
from .video_external_narration import digest

REVISION='external-performance-timing-v5-bounded-gap-placement'


def place_audio(clips, visible, anchors, duration):
    """Move existing recordings when speech fits but the silent gap does not.

    Never stretch speech, change the image, overlap another clip or discard a
    narration clip. Recognition still cannot establish the visible speaker.
    """
    from .video_manual_dub import place
    starts=[c['start'] for c in clips]
    for (_,turn),(a,b,c,d) in zip(visible,anchors):
        if b<=a or not .5<=(d-c)/(b-a)<=2:
            raise ValueError('实际口型段与配音长度差异过大，不能自动对齐')
        index=next(i for i,clip in enumerate(clips)
                   if clip['kind']=='character' and clip['character_id']==turn['character_id']
                   and clip['start']==turn['start'])
        starts[index]=round(starts[index]+a-c,6)
    shifted=place(clips,starts,duration)
    for i,clip in enumerate(shifted):
        for other in shifted[i+1:]:
            if max(clip['start'],other['start'])<min(clip['end'],other['end'])-.002:
                raise ValueError('对齐后配音会重叠，保留原时序供手动核对')
    return starts


def intervals(duration, anchors):
    points=[(0.,0.)]
    for actual_start,actual_end,target_start,target_end in anchors:
        points.extend([(actual_start,target_start),(actual_end,target_end)])
    points.append((duration,duration))
    result=[]
    for (a,c),(b,d) in zip(points,points[1:]):
        if abs(b-a)<1e-8 and abs(d-c)<1e-8:continue
        if b<=a or d<=c or b>duration or d>duration:
            raise ValueError('实际发声与配音时间不能安全对应')
        ratio=(d-c)/(b-a)
        if not .5<=ratio<=2:
            raise ValueError('口型时序偏差过大，不能用过快或过慢的画面掩盖')
        result.append(dict(start=a,end=b,target_start=c,target_end=d,scale=ratio))
    return result


def bounded_gap_anchors(duration, anchors):
    """Keep speech lengths exact while bounding neighbouring silent gaps.

    Prefer each original opening time. A shorter preceding utterance may make
    that time unsafe; move the next audio/video opening only as far as required
    by the existing 0.5–2 bounds. The final full duration must still fit.
    """
    result=[];previous_actual=previous_target=0.
    for a,b,c,d in anchors:
        if b<=a or not .5<=(d-c)/(b-a)<=2:
            raise ValueError('实际口型段与配音长度差异过大，不能自动对齐')
        gap=a-previous_actual
        if gap<0:raise ValueError('实际发声区间重叠，不能自动对齐')
        target=max(previous_target+gap*.5,min(a,previous_target+gap*2))
        end=target+d-c
        result.append((a,b,target,end))
        previous_actual,previous_target=b,end
    intervals(duration,result)
    return result


def tts_timing_match(actual, expected):
    """Timing evidence only; never relax the raw H3 dialogue quality gate.

    Written homophones cannot be distinguished from their sound. Preserve
    Mandarin tones and every syllable; missing, added or different syllables
    still fail. Lack of this optional dependency retains the strict old gate.
    """
    from .video_dialogue_quality import text_key
    actual,expected=text_key(actual),text_key(expected)
    if actual==expected:return 'normalized_text'
    if len(actual)!=len(expected):return None
    # Non-Chinese tokens (numbers, English etc.) must remain literally equal.
    for a,b in zip(actual,expected):
        if a!=b and not ('\u3400'<=a<='\u9fff' and '\u3400'<=b<='\u9fff'):
            return None
    try:
        from pypinyin import lazy_pinyin, Style
    except ImportError:
        return None
    def syllables(value):
        return lazy_pinyin(value,style=Style.TONE3,neutral_tone_with_five=True,
                           errors=lambda part:list(part))
    return 'phonetic_timing_only' if syllables(actual)==syllables(expected) else None


def speech_span(transcript,text,duration,*,evidence=None):
    from .video_dialogue_quality import text_key
    segments=transcript['segments']
    recognized=''.join(s['text'] for s in segments)
    basis=tts_timing_match(recognized,text)
    if evidence is not None:
        evidence.update(expected=text,recognized=recognized,matching_basis=basis,
                        text_verified=basis=='normalized_text',quality_accepted=False)
    if basis is None:
        raise ValueError('TTS音频识别与台词不同，不能自动推断口型位置')
    words=[w for s in segments for w in s.get('words',[]) if text_key(w['text'])]
    if not words or tts_timing_match(''.join(w['text'] for w in words),text) is None:
        raise ValueError('TTS音频缺少可核对的发声时间')
    start,end=float(words[0]['start']),float(words[-1]['end'])
    if not 0<=start<end<=duration+.08:
        raise ValueError('TTS发声时间超出录音范围')
    return start,min(end,duration)


def calibrate(path,source,data,duration,*,recognize=None):
    from .video_external_dialogue import verified
    from .video_dialogue_quality import transcribe,compare_transcript
    from .video_export import _run,ffmpeg_binary,probe_media_duration
    turns=data['plan']['turns']
    speakers={s['character_id']:s for s in data['plan']['speakers']}
    visible=[(i,t) for i,t in enumerate(turns) if t.get('mouth_visible') is not False and speakers[t['character_id']].get('on_screen',True)]
    if not visible:return source,dict(status='not_needed',lip_sync_verified=False)
    model=os.environ.get('OCV_DIALOGUE_ASR_MODEL') or os.environ.get('ASR_MODEL') or 'large-v3-turbo'
    basis=dict(version=REVISION,source=hashlib.sha256(source.read_bytes()).hexdigest(),
               plan=data['plan'],clips=data['clips'],model=model,duration=duration)
    key=digest(basis);folder=source.parent/'timing';folder.mkdir(exist_ok=True)
    receipt=folder/(key+'.json');output=folder/(key+'.mp4')
    cached_transcript=None
    if receipt.exists():
        report=json.loads(receipt.read_text(encoding='utf8'))
        if report.get('basis')!=basis:raise ValueError('口型时序校准记录与当前素材不同')
        if report['status']=='already_aligned':return source,report
        if report['status']=='audio_placed':return source,report
        if report['status']=='calibrated':
            if not output.is_file() or hashlib.sha256(output.read_bytes()).hexdigest()!=report['sha256']:
                raise ValueError('口型时序校准素材已变化，请重新检查')
            return output,report
        if report.get('status')=='unverified' and recognize is None:
            cached_transcript=report.get('transcript')
    recognize=recognize or transcribe
    report=dict(basis=basis,status='unverified',lip_sync_verified=False,speaker_verified=False)
    try:
        raw=review=None
        if cached_transcript is not None:
            try:
                # Reinterpret saved recognition with today's comparator, never
                # a persisted pass flag. A valid mismatch is reusable evidence.
                review=compare_transcript(data['plan'],cached_transcript,duration)
                raw=cached_transcript
            except (ValueError,TypeError,KeyError,IndexError):
                pass  # Malformed evidence must permit a fresh recognition.
        if review is None:
            raw=recognize(source,model)
            review=compare_transcript(data['plan'],raw,duration)
        else:
            report['native_transcript_reused']=True
        report.update(transcript=raw,review=review)
        if review['issues'] or not review['text_matches']:
            raise ValueError('H3原声与台词不能逐句可靠对应，保留原片供核对')
        clips={(c['character_id'],c['start']):c for c in data['clips'] if c['kind']=='character'}
        anchors=[]
        report['tts_reviews']=[]
        for index,turn in visible:
            clip=clips[(turn['character_id'],turn['start'])]
            audio=verified(path,clip)
            tts=recognize(audio,model)
            evidence={'character_id':turn['character_id']}
            report['tts_reviews'].append(evidence)
            a,b=speech_span(tts,clip['text'],clip['duration'],evidence=evidence)
            actual=review['turns'][index]
            anchors.append((actual['start'],actual['end'],clip['start']+a,clip['start']+b))
        report.update(anchors=anchors)
        if any(r['matching_basis']=='phonetic_timing_only' for r in report['tts_reviews']):
            report['tts_timing_note']='同音同调识别仅用于录音发声定位；文字、说话者与口型未据此验收'
        try:
            pieces=intervals(duration,anchors)
        except ValueError:
            # Large silent gaps do not imply a bad speaking performance. Move
            # short, matching recordings rather than speeding up the picture.
            starts=place_audio(data['clips'],visible,anchors,duration)
            report['audio_starts']=starts
            if all(abs((b-a)-(d-c))<=.15 for a,b,c,d in anchors):
                report.update(status='audio_placed',
                              warning='已按实际发声位置移动已有配音，画面未变；说话者与口型仍需观看核对')
                temporary=receipt.with_suffix('.tmp');temporary.write_text(json.dumps(report,ensure_ascii=False),encoding='utf8');temporary.replace(receipt)
                return source,report
            # Keep actual speech starts; only bounded speech length correction
            # and adjacent gaps remain, rather than speeding a long idle gap.
            report['original_anchors']=anchors
            preferred=[(a,b,a,a+d-c) for a,b,c,d in anchors]
            try:
                pieces=intervals(duration,preferred)
                anchors=preferred
            except ValueError:
                bounded=bounded_gap_anchors(duration,anchors)
                # Derive recording starts from original TTS speech onsets,
                # then validate full clips, including their leading silence.
                adjusted=[(target,target+b-a,c,d) for (a,b,c,d),(_,_,target,_) in zip(anchors,bounded)]
                report['audio_starts']=place_audio(data['clips'],visible,adjusted,duration)
                anchors=bounded
                pieces=intervals(duration,anchors)
            report['anchors']=anchors
        report.update(intervals=pieces)
        if all(abs(a-c)<.033 and abs(b-d)<.033 for a,b,c,d in anchors):
            report['status']='already_aligned'
            temporary=receipt.with_suffix('.tmp');temporary.write_text(json.dumps(report,ensure_ascii=False),encoding='utf8');temporary.replace(receipt)
            return source,report
        filters=[];labels=[]
        for index,piece in enumerate(pieces):
            label=f'part{index}';labels.append(f'[{label}]')
            filters.append(f'[0:v]trim=start={piece["start"]:.9f}:end={piece["end"]:.9f},'
                f'setpts=(PTS-STARTPTS)*{piece["scale"]:.9f},fps=30,'
                f'tpad=stop_mode=clone:stop_duration=0.1,trim=duration={piece["target_end"]-piece["target_start"]:.9f},'
                f'setpts=PTS-STARTPTS[{label}]')
        filters.append(''.join(labels)+f'concat=n={len(labels)}:v=1:a=0[video]')
        temp=output.with_suffix('.tmp.mp4')
        _run([ffmpeg_binary(),'-y','-i',str(source),'-filter_complex_threads','1',
              '-filter_complex',';'.join(filters),'-map','[video]','-an',
              '-c:v','libx264','-preset','fast','-crf','18','-threads','2',str(temp)],'按实际发声位置校准原画面，保留TTS音频')
        if abs(probe_media_duration(temp)-duration)>.12:
            raise ValueError('口型校准画面时长检查未通过')
        temp.replace(output)
        report.update(status='calibrated',sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    except Exception as exc:
        # Recognition cannot prove a mouth or speaker. Keep the usable source,
        # expose the limitation, and never silently mark alignment as passed.
        report.update(status='unverified',warning=str(exc)[:1200])
        report.pop('audio_starts',None)
    temporary=receipt.with_suffix('.tmp');temporary.write_text(json.dumps(report,ensure_ascii=False),encoding='utf8');temporary.replace(receipt)
    return (output if report['status']=='calibrated' else source),report
