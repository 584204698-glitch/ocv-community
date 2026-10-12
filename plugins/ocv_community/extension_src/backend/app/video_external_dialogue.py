"""Exact per-speaker TTS, measured visual guide and declared post-dub track."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
import subprocess
import sys
from pathlib import Path

from .tts_emotion import resolve
from .video_external_narration import asset, digest, synthesize, wav_seconds

REVISION = 'external-dialogue-v1'


def engine(record):
    value = (record.get('creation_parameters') or {}).get('dialogue_tts_engine', 'indextts25')
    if value not in ('indextts25', 'qwen3'):
        raise ValueError('未知对白配音引擎，请重新选择配音路线')
    return value


def qwen_reference(owner, voice):
    from .qwen3_local import resolve_saved_voice
    reference, text = resolve_saved_voice(voice['voice_id'], owner)
    if hashlib.sha256(reference.read_bytes()).hexdigest() != voice['sha256']:
        raise ValueError('已采用的音色发生变化，请刷新音色绑定后重试')
    return reference, text


def synthesize_qwen(owner, text, voice, folder):
    qwen_reference(owner, voice)
    folder.mkdir(parents=True, exist_ok=True)
    # A failed attempt retains its diagnostics; retry uses a fresh run directory.
    run = folder / ('run-' + uuid.uuid4().hex)
    text_file=folder/'text.txt'
    text_file.write_text(text,encoding='utf8')
    app=Path(__file__).resolve().parents[2]
    command=[sys.executable,'-B','-m','backend.app.qwen_dialogue_synthesis',
             '--owner',str(owner),'--voice-id',voice['voice_id'],'--voice-sha',voice['sha256'],
             '--text-file',str(text_file),'--output',str(run)]
    with (folder / 'synthesis.log').open('a', encoding='utf8') as log:
        result=subprocess.run(command,cwd=app,stdout=log,stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError('Qwen 对白合成失败，已保留本次日志；请检查模型与计算队列后重试本镜')
    output = run / 'chunk-0001.wav'
    wav_seconds(output)
    return output


def enabled(record):
    return (record.get('creation_parameters') or {}).get('dialogue_route') == 'external_tts'


def verified(path, clip):
    file = asset(path, clip['file'])
    if hashlib.sha256(file.read_bytes()).hexdigest() != clip['sha256']:
        raise ValueError('角色配音缓存已变化，请重新生成本镜配音')
    if abs(wav_seconds(file)-clip['duration']) > .002:
        raise ValueError('角色配音时长记录与实际音频不一致')
    return file


def _without_narration_cue(action):
    """Remove only explicit temporal wrappers, never the action they introduce.

    Match at the outer edges of a beat, not inside quoted dialogue or arbitrary
    sentences mentioning a narrator. Unknown prose remains unchanged.
    """
    action = re.sub(
        r'^(?:(?:As|While) the narrator speaks|During (?:the )?narration|'
        r'(?:As|When) the narration (?:starts|begins|ends))\s*,\s*(?=\S)',
        '', action, flags=re.I)
    action = re.sub(
        r'^(?:旁白(?:讲述|讲解|响起|开始|结束|进行)时|在旁白(?:讲述|讲解|进行)期间|'
        r'随着旁白(?:响起|开始|结束))[，,]\s*(?=\S)', '', action)
    return re.sub(r'\s+(?:as|when) the narration (?:starts|begins|ends)([.!?]?)$',
                  r'\1', action, flags=re.I)


def _visual_beats(visual):
    # Mouth posture and allowed nonverbal actions already have shared compiler
    # rules. Add only a short, local no-words cue outside character speech.
    for beat in visual.get('performance_beats', []):
        silent = not any(max(beat['start'], turn['start']) < min(beat['end'], turn['end'])
                         for turn in visual['turns'])
        for key, prefix in (('action', 'Without speaking: '), ('action_zh', '不说台词：')):
            action = _without_narration_cue(beat[key])
            beat[key] = prefix + action if silent and not action.startswith(prefix) else action


def retime(plan, clips):
    """Keep approved events/gaps; speech slots follow actual WAVs, no stretching."""
    full = copy.deepcopy(plan)
    mappings = []; shift = 0.0
    for index, turn in enumerate(full['turns']):
        start, end = turn['start'], turn['end']
        length = clips[index]['duration']+.15
        mappings.append((start, end, start+shift, start+shift+length))
        shift += length-(end-start)
    duration = max(4, math.ceil(plan['duration']+shift-1e-6))
    if duration > 15:
        raise ValueError(f'实测对白与动作需要约 {duration} 秒，超过H3单镜15秒上限。配音已缓存，请拆分本镜或缩短停顿；未截断台词。')
    def warp(time):
        delta = 0.0
        for a,b,c,d in mappings:
            if time<a:return round(time+delta,6)
            if time<=b:return round(c+(time-a)/(b-a)*(d-c),6)
            delta=d-b
        return round(time+delta,6)
    for turn in full['turns']:
        turn['start'],turn['end'] = warp(turn['start']),warp(turn['end'])
    for beat in full.get('performance_beats',[]):
        beat['start'],beat['end'] = warp(beat['start']),warp(beat['end'])
    if full.get('performance_beats'):full['performance_beats'][-1]['end']=duration
    full['duration']=duration
    by_id={s['character_id']:s for s in full['speakers']}
    timed=[]
    for index, turn in enumerate(full['turns']):
        clip=clips[index]
        timed.append({**clip,'character_id':turn['character_id'],'text':turn['text'],
            'kind':by_id[turn['character_id']]['kind'],'start':turn['start'],
            'end':round(turn['start']+clip['duration'],6)})
    visual=copy.deepcopy(full)
    visual['speakers']=[s for s in visual['speakers'] if s['kind']!='narrator']
    ids={s['character_id'] for s in visual['speakers']}
    visual['turns']=[t for t in visual['turns'] if t['character_id'] in ids]
    # The hand-off margin belongs to silence, not to the speaking mouth.
    for turn, clip in zip(full['turns'], timed):
        if turn['character_id'] in ids:
            next(t for t in visual['turns'] if t['character_id']==turn['character_id']
                 and t['start']==turn['start'])['end'] = clip['end']
            beats=[]
            for beat in visual.get('performance_beats',[]):
                if beat['start'] < clip['end'] < beat['end']:
                    beats.extend([{**beat,'end':clip['end']},{**beat,'start':clip['end'],
                        'action':'Maintain the preceding physical end-state with the speaking mouth at rest. No new action.',
                        'action_zh':'保持上一动作结束时的姿态，嘴部自然闭合，不追加动作。'}])
                else:beats.append(beat)
            if beats:visual['performance_beats']=beats
    _visual_beats(visual)
    from .h3_dialogue import DialoguePlan
    return DialoguePlan.model_validate(visual).model_dump(),timed


def track_arguments(path, clips, duration, *, narrator_volume=1):
    inputs=['-f','lavfi','-i',f'anullsrc=r=48000:cl=stereo:d={duration:g}']
    filters=['[0:a]asetpts=PTS-STARTPTS[base]']; labels=['[base]']
    for index,clip in enumerate(clips,1):
        file=verified(path,clip)
        if clip['start']<0 or clip['end']>duration+.002:
            raise ValueError('实测配音超出画面时间范围，未截断音频')
        inputs+=['-i',str(file)]
        volume=narrator_volume if clip['kind']=='narrator' else 1
        filters.append(f'[{index}:a]aresample=48000,asetpts=PTS-STARTPTS,volume={volume:g},'
            f'adelay={round(clip["start"]*48000)}S:all=1[line{index}]')
        labels.append(f'[line{index}]')
    filters.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=first:normalize=0:'
        'dropout_transition=0,alimiter=limit=0.95:level=0:latency=1[track]')
    return inputs,';'.join(filters)


def make_track(path, folder, clips, duration, *, narrator_volume=1):
    from .video_export import _run, ffmpeg_binary
    key=digest(dict(version=REVISION,clips=clips,duration=duration,narrator_volume=narrator_volume))
    file=folder/f'track-{key}.wav'
    if not file.exists():
        folder.mkdir(parents=True,exist_ok=True)
        inputs,graph=track_arguments(path,clips,duration,narrator_volume=narrator_volume)
        temp=file.with_suffix('.tmp.wav')
        _run([ffmpeg_binary(),'-y',*inputs,'-filter_complex_threads','1','-filter_complex',graph,'-map','[track]',
            '-threads','2','-c:a','pcm_s16le',str(temp)],'角色对白按实测时序合成')
        if abs(wav_seconds(temp)-duration)>.002:raise ValueError('配音时间轴时长验证失败')
        temp.replace(file)
    return dict(file=file.relative_to(Path(path)).as_posix(),sha256=hashlib.sha256(file.read_bytes()).hexdigest(),duration=wav_seconds(file))


def prepare(owner,path,record,plan,voices,*,allow_synthesis=False):
    if not enabled(record):return None
    selected_engine=engine(record)
    by_id={v['character_id']:v for v in voices}; clips={}
    for index,turn in enumerate(plan['turns']):
        voice=by_id.get(turn['character_id'])
        if not voice:
            raise ValueError('精确对白需要为每位发言者采用固定音色，不能使用H3自行生成声音')
        emotion=resolve(turn)
        parameters={k:emotion[k] for k in ('emotion','strength')}
        basis=dict(version=REVISION,owner=owner,project=record['id'],
            text=turn['text'],voice_id=voice['voice_id'],voice_sha=voice['sha256'],emotion=parameters)
        if selected_engine=='qwen3':
            _, reference_text=qwen_reference(owner,voice)
            parameters={'mode':'reference_expression'}
            basis.update(engine=selected_engine,emotion=parameters,reference_text=reference_text)
        key=digest(basis)
        folder=Path(path)/'assets/dialogue'/key; receipt=folder/'receipt.json'
        if receipt.exists():
            clip=json.loads(receipt.read_text(encoding='utf8'))
            if clip['key']!=key:raise ValueError('角色配音缓存身份不一致')
            verified(path,clip)
        elif not allow_synthesis:
            return None
        else:
            file=(synthesize_qwen(owner,turn['text'],voice,folder) if selected_engine=='qwen3'
                  else synthesize(owner,path,turn['text'],voice,folder,emotion=emotion))
            clip=dict(key=key,file=file.relative_to(Path(path)).as_posix(),
                sha256=hashlib.sha256(file.read_bytes()).hexdigest(),duration=wav_seconds(file),
                voice_id=voice['voice_id'],voice_sha256=voice['sha256'],emotion=parameters,
                engine=selected_engine)
            temp=receipt.with_suffix('.tmp')
            temp.write_text(json.dumps(clip,ensure_ascii=False),encoding='utf8');temp.replace(receipt)
        clips[index]=clip
    visual,timed=retime(plan,clips)
    track_key=digest(dict(version=REVISION,clips=timed,plan=visual))
    folder=Path(path)/'assets/dialogue_tracks'/track_key; receipt=folder/'receipt.json'
    if receipt.exists():
        result=json.loads(receipt.read_text(encoding='utf8'))
        if result['key']!=track_key or result['clips']!=timed or result['plan']!=visual:
            raise ValueError('配音时间轴缓存与当前分镜不一致')
        verified(path,result['guide'])
        return result
    if not allow_synthesis:return None
    # Narration is absent from mouth guidance, including in mixed stories.
    guide=make_track(path,folder,[c for c in timed if c['kind']=='character'],visual['duration'])
    result=dict(version=REVISION,key=track_key,plan=visual,clips=timed,guide=guide)
    temp=receipt.with_suffix('.tmp')
    temp.write_text(json.dumps(result,ensure_ascii=False),encoding='utf8');temp.replace(receipt)
    return result


def preview(path,shot,volume=.65):
    from . import video_export as export
    data=(shot.get('video_request') or {}).get('external_dialogue')
    if not data:raise ValueError('本镜缺少冻结的精确对白音频')
    source=export._source(path,shot)
    original_source=source
    duration=export.probe_media_duration(source)
    if duration+.03<data['plan']['duration']:raise ValueError('画面长度不足以承载完整对白')
    if shot.get('manual_dub_timing'):
        from .video_manual_dub import effective_clips
        clips=effective_clips(shot,source,data,duration)
        alignment=dict(status='manual',lip_sync_verified=False,speaker_verified=False)
    else:
        from .video_external_alignment import calibrate
        source,alignment=calibrate(path,source,data,duration)
        clips=data['clips']
        if alignment.get('audio_starts') is not None:
            from .video_manual_dub import place,basis
            clips=place(clips,alignment['audio_starts'],duration)
            alignment['audio_basis']=basis(original_source,data)
    shot['dialogue_alignment']={k:alignment.get(k) for k in ('status','warning','anchors','audio_starts','audio_basis','lip_sync_verified','speaker_verified') if k in alignment}
    track=make_track(path,source.parent,clips,duration,narrator_volume=volume)
    from .video_environment_audio import mix_environment
    track,environment=mix_environment(path,original_source,source,track,duration,alignment)
    if environment is not None:
        shot['environment_audio']=environment
    else:
        shot.pop('environment_audio',None)
    output=source.parent/f'dubbed-{digest(dict(source=hashlib.sha256(source.read_bytes()).hexdigest(),track=track))}.mp4'
    environment_receipt=output.with_suffix('.environment.json')
    if environment is not None and output.exists():
        if not environment_receipt.is_file():
            raise ValueError('环境声配音缓存缺少音画哈希记录，未冒充已核验输出')
        saved=json.loads(environment_receipt.read_text(encoding='utf8'))
        if (saved.get('environment')!=environment or saved.get('track')!=track
                or saved.get('video_sha256')!=hashlib.sha256(output.read_bytes()).hexdigest()):
            raise ValueError('环境声配音缓存与当前音画文件不一致')
    if not output.exists():
        temp=output.with_suffix('.tmp.mp4')
        export._run([export.ffmpeg_binary(),'-y','-i',str(source),'-i',str(verified(path,track)),
            '-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k',
            '-threads','2','-movflags','+faststart',str(temp)],'采用TTS对白音轨，保留H3原片')
        if abs(export.probe_media_duration(temp)-duration)>.1:raise ValueError('对白预览时长校验失败')
        temp.replace(output)
        if environment is not None:
            from .video_environment_audio import _write_json
            _write_json(environment_receipt,dict(environment=environment,track=track,
                video_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),quality_accepted=False))
    return output.relative_to(Path(path)).as_posix()
