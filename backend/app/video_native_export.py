"""Native H3 dialogue assembly, without a fabricated narration track."""
from __future__ import annotations

import json
import math
from pathlib import Path
import uuid


def _stamp(value):
    milliseconds = round(value * 1000)
    seconds, ms = divmod(milliseconds, 1000)
    minutes, sec = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f'{hour:02d}:{minute:02d}:{sec:02d},{ms:03d}'


def render_native(path, record):
    from . import video_export as export
    from .h3_dialogue import DialoguePlan
    from .video_dialogue import assert_native_export_current
    path = Path(path).resolve()
    legacy = assert_native_export_current(int(path.parent.name), path, record,
        observation_only=record.get('_native_observation_export') is True)
    from .video_external_dialogue import enabled as exact_enabled,verified
    exact=exact_enabled(record)
    if not exact and not (record.get('export_settings') or {}).get('use_video_audio', True):
        raise ValueError('原生对话需要保留 H3 音轨，不能关闭原声后冒充完成')
    sources, segments, captions, total_frames = [], [], [], 0
    reports = record.get('_native_speech_reviews')
    has_external=any((s.get('video_request') or {}).get('external_narration') for s in record['shots'])
    if has_external and reports is None:
        # Even observation exports must not silently mix over misplaced speech.
        from .video_dialogue_quality import check_native_speech
        reports=check_native_speech(path,record)
    if reports is not None and len(reports) != len(record['shots']):
        raise ValueError('对白检查结果与镜头数量不一致')
    for index, shot in enumerate(record['shots']):
        source = export._source(path, shot)
        if not exact and not export._has_audio_stream(source):
            raise ValueError(f'第{index+1}镜缺少 H3 原生音轨，请重新生成或核对下载结果')
        duration = export.probe_media_duration(source)
        if not math.isfinite(duration) or not 0 < duration < 17:
            raise ValueError(f'第{index+1}镜的实际时长无效')
        # Retain the entire generated utterance. H3 snaps duration to its own
        # frame grid; never crop a 6.58s performance to a planned 6.00s subtitle.
        frames = math.ceil(duration * 30 - 1e-6)
        plan = DialoguePlan.model_validate(shot['video_request']['dialogue']['plan'])
        turns = [t.model_dump() for t in plan.turns]
        if exact:
            data=shot['video_request'].get('external_dialogue')
            if not data:raise ValueError('本镜缺少冻结的TTS对白音频')
            verified(path,data['guide'])
            from .video_manual_dub import effective_clips
            turns=effective_clips(shot,source,data,duration)
            for clip in turns:verified(path,clip)
        elif reports is not None:
            review = reports[index]['review']
            if review['issues'] or not review['text_matches']:
                raise ValueError('本次对白筛查未通过，不能自动导出')
            turns = review['turns']
        narration=(shot['video_request'].get('external_narration') or {}).get('clips',[])
        if narration:
            from .video_external_narration import check_windows
            check_windows(narration,turns,duration)
            turns=sorted([*turns,*narration],key=lambda t:t['start'])
        for turn in turns:
            if turn['end'] > duration + .05:
                raise ValueError(f'第{index+1}镜结束早于计划台词，请检查原声')
            captions.append({'text': turn['text'], 'character_id': turn['character_id'],
                'start': total_frames / 30 + turn['start'], 'end': total_frames / 30 + min(turn['end'], duration)})
        sources.append(source)
        segments.append({'frames': frames, 'duration': frames / 30,'narration':narration,'exact':exact,'shot':shot})
        total_frames += frames
    expected = total_frames / 30
    run_id = uuid.uuid4().hex
    work = path / 'export_work' / run_id
    root = export.PROJECT_ROOT / 'output' / (
        f'{export._safe_name(record["settings"].get("name"))}_原生对话_'
        f'{export._safe_name(path.parent.name)}_{export._safe_name(path.name)}_{run_id}')
    work.mkdir(parents=True); root.mkdir(parents=True)
    width, height = export._size(record)
    clips = []
    for index, (source, segment) in enumerate(zip(sources, segments), 1):
        if segment['exact']:
            from .video_external_dialogue import preview
            source=path/preview(path,segment['shot'],volume=export._narration_volume(record))
        if segment['narration']:
            from .video_external_narration import mix_arguments
            inputs,graph=mix_arguments(path,source,segment['narration'],export._narration_volume(record))
            mixed=work/f'{index:04d}-narrated.mkv'
            export._run([export.ffmpeg_binary(),'-y',*inputs,'-filter_complex',graph,
                '-map','0:v:0','-map','[mixed]','-c:v','copy','-c:a','pcm_s16le',str(mixed)],'旁白与原生环境声按时间线混合')
            source=mixed
        clip = work / f'{index:04d}.mov'
        vf = (f'setpts=PTS-STARTPTS,fps=30,tpad=stop_mode=clone:stop_duration=0.05,'
              f'trim=end_frame={segment["frames"]},setpts=N/(30*TB),'
              f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
              f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p')
        if index == len(sources):
            vf += export._end_title_filter(record, work, width, height, segment['duration'])
        af = ('aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS,'
              f'apad,atrim=end_sample={segment["frames"]*1600}')
        export._run([export.ffmpeg_binary(), '-y', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0',
            '-vf', vf, '-af', af, '-t', f'{segment["duration"]:.9f}', '-c:v', 'libx264', '-preset', 'fast',
            '-crf', '18', '-c:a', 'pcm_s16le', str(clip)], f'第{index}镜原生音画标准化')
        clips.append(clip)
    concat = work / 'concat.txt'
    concat.write_text('\n'.join("file '" + p.as_posix().replace("'", "'\\''") + "'" for p in clips), encoding='utf8')
    raw = root / '最终视频_纯净版.mp4'
    export._run([export.ffmpeg_binary(), '-y', '-f', 'concat', '-safe', '0', '-i', str(concat),
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(raw)], '原生音画拼接')
    srt = root / '最终字幕.srt'
    srt.write_text('\n\n'.join(f'{i}\n{_stamp(r["start"])} --> {_stamp(r["end"])}\n{r["text"]}'
                                for i, r in enumerate(captions, 1)) + '\n', encoding='utf8')
    subtitled = root / '最终视频_字幕版.mp4'
    ass = export._subtitle_ass(srt, work / 'subtitles.ass', width, height)
    subtitle_filter = f"subtitles=filename='{export._subtitle_filter_path(ass)}':charenc=UTF-8:force_style='{export._subtitle_style(record,height)}'"
    export._run([export.ffmpeg_binary(), '-y', '-i', str(raw), '-vf', subtitle_filter, '-c:v', 'libx264',
        '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', str(subtitled)], '原生对白字幕合成')
    (root/'动态分镜方案.json').write_text(json.dumps(record['shots'], ensure_ascii=False, indent=2), encoding='utf8')
    engines=sorted({c.get('engine','indextts25') for s in record['shots'] for c in (s.get('video_request',{}).get('external_dialogue') or {}).get('clips',[])})
    (root/'声音与字幕说明.json').write_text(json.dumps({'audio_source':('TTS post-dub ('+', '.join(engines)+'); original H3 audio excluded') if exact else 'H3 character dialogue and ambience + measured IndexTTS2.5 narration' if has_external else 'H3 native audio',
        'subtitle_timing':'measured_tts_clips' if exact else 'asr_word_spans' if reports is not None else 'planned_observation_only',
        'legacy_voice_reference_shots':legacy or [],
        'speaker_and_timbre_verified':False,'quality_accepted':False,'captions':captions}, ensure_ascii=False, indent=2), encoding='utf8')
    for output in (raw, subtitled):
        if abs(export.probe_media_duration(output) - expected) > .1:
            raise RuntimeError('原生对话成片时长校验失败')
    return root, raw, subtitled
