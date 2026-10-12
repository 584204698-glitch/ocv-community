"""Measured, project-owned narration; never sent to H3 as words or references."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import uuid
import wave

REVISION = 'external-narration-v1'


def enabled(record):
    return (record.get('creation_parameters') or {}).get('narration_route') == 'external_tts'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def asset(path, relative):
    root=Path(path).resolve(); target=(root/relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError('独立旁白文件缺失，请重新生成本镜以补齐旁白')
    return target


def wav_seconds(file):
    with wave.open(str(file),'rb') as audio:
        seconds=audio.getnframes()/audio.getframerate()
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('独立旁白音频时长无效')
    return seconds


def synthesize(owner, root, text, voice, folder, *, emotion=None):
    """Use the existing guarded IndexTTS runner, with no story/model rewrite."""
    app=Path(__file__).resolve().parents[2]
    folder.mkdir(parents=True,exist_ok=True)
    script=folder/'text.txt';chunks=folder/'chunks.json'
    script.write_text(text,encoding='utf8')
    chunks.write_text(json.dumps([text],ensure_ascii=False),encoding='utf8')
    command=[sys.executable,str(app/'module1_agent_director.py'),'--text',str(script),
        '--job-id','narration_'+uuid.uuid4().hex,'--tts-engine','indextts25',
        '--user-id',str(owner),'--tts-voice-path',str(voice['path']),'--chunks-json',str(chunks),
        '--output-dir',str(folder/'output'),'--segment-archive-dir',str(folder/'segments'),
        '--tts-speed','1','--tts-volume','1','--tts-parallelism','1']
    if emotion is not None:
        from .tts_emotion import runner_arguments
        command += runner_arguments(emotion)
    with (folder/'synthesis.log').open('w',encoding='utf8') as log:
        result=subprocess.run(command,cwd=app,env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT)
    if result.returncode:
        raise ValueError('独立旁白合成失败，已保留原素材及合成日志；检查 IndexTTS2.5 服务后重试本镜')
    output=folder/'segments/segment_0001.wav'
    wav_seconds(output)
    return output


def retime(plan, clips):
    """Preserve actions/order; replace narration speech slots by measured length.

    Inter-turn quiet gaps stay intact; a small hand-off margin follows narration.
    No audio time-stretch, new creative events, or automatic story splitting.
    """
    full=copy.deepcopy(plan)
    mappings=[]; shift=0.0
    for index,turn in enumerate(full['turns']):
        old_start,old_end=turn['start'],turn['end']
        if index in clips:
            duration=max(old_end-old_start,clips[index]['duration']+.15)
        else:
            duration=old_end-old_start
        mappings.append((old_start,old_end,old_start+shift,old_start+shift+duration))
        shift+=duration-(old_end-old_start)
    duration=math.ceil(float(plan['duration'])+shift-1e-6)
    if duration>15:
        raise ValueError(f'实测旁白需要本镜约 {duration} 秒，超过 H3 的15秒上限。旁白已缓存；请在本镜二次编辑中拆分内容或缩短非必要停顿，不必重做音色')
    def warp(time):
        delta=0.0
        for a,b,c,d in mappings:
            if time<a:return round(time+delta,6)
            if time<=b:return round(c+(time-a)/(b-a)*(d-c),6)
            delta=d-b
        return round(time+delta,6)
    for turn in full['turns']:
        turn['start'],turn['end']=warp(turn['start']),warp(turn['end'])
    for beat in full.get('performance_beats',[]):
        beat['start'],beat['end']=warp(beat['start']),warp(beat['end'])
    if full.get('performance_beats'):
        full['performance_beats'][-1]['end']=duration
    full['duration']=duration
    projected=copy.deepcopy(full)
    narrator_ids={s['character_id'] for s in full['speakers'] if s['kind']=='narrator'}
    projected['speakers']=[s for s in full['speakers'] if s['character_id'] not in narrator_ids]
    projected['turns']=[t for t in full['turns'] if t['character_id'] not in narrator_ids]
    narration=[]
    for index,clip in clips.items():
        turn=full['turns'][index]
        narration.append({**clip,'text':turn['text'],'character_id':turn['character_id'],
                          'start':turn['start'],'end':round(turn['start']+clip['duration'],6)})
    from .h3_dialogue import DialoguePlan
    return DialoguePlan.model_validate(projected).model_dump(),narration


def prepare(owner,path,record,plan,voices,*,allow_synthesis=False):
    if not enabled(record):return None
    speakers={s['character_id']:s for s in plan['speakers']}
    if not any(speakers[t['character_id']]['kind']=='narrator' for t in plan['turns']):return None
    by_id={v['character_id']:v for v in voices}
    clips={}
    for index,turn in enumerate(plan['turns']):
        if speakers[turn['character_id']]['kind']!='narrator':continue
        voice=by_id.get(turn['character_id'])
        if not voice:
            raise ValueError('独立旁白需要已采用的固定音色，请在旁白音色设置中采用声音；角色仍可使用 H3 自行生成')
        from .tts_emotion import resolve
        emotion=resolve(turn)
        basis=dict(version=REVISION,owner=owner,project=record['id'],text=turn['text'],
                   voice_id=voice['voice_id'],voice_sha=voice['sha256'])
        # Unchanged natural narration retains its existing cached audio.
        if emotion['emotion']!='neutral':
            basis['emotion']={key:emotion[key] for key in ('emotion','strength')}
        key=digest(basis)
        folder=Path(path)/'assets/narration'/key; receipt=folder/'receipt.json'
        if receipt.is_file():
            clip=json.loads(receipt.read_text(encoding='utf8'))
            file=asset(path,clip['file'])
            if clip.get('key')!=key or hashlib.sha256(file.read_bytes()).hexdigest()!=clip.get('sha256'):
                raise ValueError('独立旁白缓存已变化，请核对音频文件')
            if abs(wav_seconds(file)-clip['duration'])>.002:
                raise ValueError('独立旁白时长记录与音频不一致')
        elif not allow_synthesis:
            return None  # Read-only preflight never triggers inference.
        else:
            file=(synthesize(owner,path,turn['text'],voice,folder,emotion=emotion)
                  if emotion['emotion']!='neutral' else synthesize(owner,path,turn['text'],voice,folder))
            clip=dict(key=key,file=file.relative_to(Path(path)).as_posix(),
                      sha256=hashlib.sha256(file.read_bytes()).hexdigest(),duration=wav_seconds(file))
            temporary=receipt.with_suffix('.tmp')
            temporary.write_text(json.dumps(clip,ensure_ascii=False),encoding='utf8');temporary.replace(receipt)
        clips[index]=clip
    projected,narration=retime(plan,clips)
    return dict(version=REVISION,plan=projected,clips=narration)


def check_windows(narration,actual_turns,duration):
    for clip in narration:
        if clip['end']>duration+.03:raise ValueError('独立旁白超出本镜实际时长，未截断音频')
        for turn in actual_turns:
            if min(clip['end']+.1,turn['end'])>max(clip['start']-.1,turn['start']):
                raise ValueError('H3实际对白侵入旁白时段，未强行混音。请局部调整本镜发言间隙或重做本镜')


def mix_arguments(path,source,narration,volume):
    """Mix into original ambience; never mute or replace H3's source track."""
    inputs=['-i',str(source)];filters=['[0:a:0]aresample=48000,asetpts=PTS-STARTPTS[original]'];labels=['[original]']
    for i,clip in enumerate(narration,1):
        file=asset(path,clip['file'])
        if hashlib.sha256(file.read_bytes()).hexdigest()!=clip['sha256']:raise ValueError('旁白音频已变化，未导出')
        inputs+=['-i',str(file)]
        filters.append(f'[{i}:a:0]aresample=48000,asetpts=PTS-STARTPTS,volume={volume:g},adelay={round(clip["start"]*48000)}S:all=1[n{i}]')
        labels.append(f'[n{i}]')
    filters.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95:level=0:latency=1[mixed]')
    return inputs,';'.join(filters)


def preview(path,shot,volume=.65):
    clips=((shot.get('video_request') or {}).get('external_narration') or {}).get('clips',[])
    if not clips:return None
    from . import video_export as export
    from .video_dialogue_quality import screen_dialogue
    source=asset(path,shot['video'])
    report=screen_dialogue(path,shot)['review']
    if report['issues'] or not report['text_matches']:
        raise ValueError('原片已保留，旁白混音前需核对H3异常语音：'+str(report.get('recognized',''))[:240])
    check_windows(clips,report['turns'],export.probe_media_duration(source))
    key=digest(dict(clips=clips,source=hashlib.sha256(source.read_bytes()).hexdigest(),volume=volume,version=REVISION))[:16]
    output=source.with_name('narrated-'+key+'.mp4')
    if not output.is_file():
        inputs,graph=mix_arguments(path,source,clips,volume)
        temporary=output.with_name(output.stem+'.tmp.mp4')
        export._run([export.ffmpeg_binary(),'-y',*inputs,'-filter_complex',graph,
            '-map','0:v:0','-map','[mixed]','-c:v','copy','-c:a','aac','-b:a','192k',
            '-movflags','+faststart',str(temporary)],'生成本镜独立旁白预览')
        temporary.replace(output)
    return output.relative_to(Path(path)).as_posix()
