"""Local final assembly for dynamic-video projects."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import uuid
import tempfile
import base64
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from .subtitle_layout import normalize as normalize_layout, write_ass

from . import video_studio as studio
from .pipeline import (ffmpeg_binary, ffprobe_binary, probe_media_duration, _subtitle_filter_path,
                       persist_step_workflow_state, register_job_asset, store)

router = APIRouter(prefix='/api/video-studio')
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ExportRequest(BaseModel):
    revision: int
    use_video_audio: bool = True
    narration_volume: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    end_title_text: str | None = Field(default=None, max_length=80)
    subtitle_layouts: dict | None = None


def _narration_volume(record):
    value = float((record.get('export_settings') or {}).get('narration_volume', 1.0))
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('旁白音量必须在 0% 到 100% 之间')
    return value


class LayoutPreviewRequest(BaseModel):
    subtitle_layouts: dict
    shot_id: str
    text: str = '从一段文字开始，让每个想法被看见。'
    video_render_variant: str = 'both'


def presentation(record, layouts=None):
    params = record.get('creation_parameters') or {}
    return normalize_layout(dict(
        video_orientation='portrait' if record.get('settings', {}).get('ratio') == '9:16' else 'landscape',
        subtitle_layouts=layouts if layouts is not None else params.get('subtitle_layouts', {})))


def frame_filter(layout):
    w, h, scale = layout['width'], layout['height'], layout['frame_scale']
    sw, sh = max(2, round(w*scale/2)*2), max(2, round(h*scale/2)*2)
    return (f'setpts=PTS-STARTPTS,scale={sw}:{sh}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[placed];'
            f'color=c=black:s={w}x{h}:r=30[canvas];'
            f'[canvas][placed]overlay=x={w*layout["frame_x"]/100:.3f}-overlay_w/2:'
            f'y={h*layout["frame_y"]/100:.3f}-overlay_h/2:shortest=1,fps=30,format=yuv420p')


def _video_duration(source):
    """Use the video track, since container duration may belong to longer audio."""
    result = subprocess.run([ffprobe_binary(), '-v', 'error', '-select_streams', 'v:0',
                             '-show_entries', 'stream=duration', '-of', 'json', str(source)],
                            capture_output=True, text=True, check=True)
    streams = json.loads(result.stdout).get('streams', [])
    if not streams:
        raise ValueError(f'动态素材没有视频轨：{source.name}')
    try:
        duration = float(streams[0].get('duration', ''))
        if duration > 0:
            return duration
    except (TypeError, ValueError):
        pass
    # Some containers (notably MKV) omit stream duration. Packet timestamps
    # remain authoritative; do not silently revert to the audio/container.
    result = subprocess.run([ffprobe_binary(), '-v', 'error', '-select_streams', 'v:0',
                             '-show_packets', '-show_entries', 'packet=pts_time,duration_time',
                             '-of', 'json', str(source)], capture_output=True, text=True, check=True)
    spans = [(float(p['pts_time']), float(p.get('duration_time') or 0))
             for p in json.loads(result.stdout).get('packets', []) if 'pts_time' in p]
    if not spans:
        raise ValueError(f'无法读取动态素材的视频时长：{source.name}')
    duration = max(start + length for start, length in spans) - min(start for start, _ in spans)
    if duration <= 0:
        raise ValueError(f'动态素材视频时长无效：{source.name}')
    return duration


@router.post('/{identity}/layout-preview')
def layout_preview(identity: str, data: LayoutPreviewRequest, request: Request):
    path = studio.directory(studio.require_user(request)['id'], identity)
    record = studio.read(path)
    shot = next((s for s in record.get('shots', []) if s['id'] == data.shot_id), None)
    if not shot:
        raise HTTPException(404, '预览镜头不存在')
    try:
        source = _asset(path, shot.get('image') or shot.get('video'))
    except ValueError as exc:
        raise HTTPException(409, '此镜头尚无可用于预览的素材，请先生成或替换图片。') from exc
    layout = presentation(record, data.subtitle_layouts)
    with tempfile.TemporaryDirectory(prefix='ocv-layout-preview-') as folder:
        root = Path(folder)
        srt = root / 'preview.srt'
        srt.write_text('1\n00:00:00,000 --> 00:00:05,000\n'+data.text[:160]+'\n', encoding='utf-8')
        ass = write_ass(srt, root / 'preview.ass', layout)
        vf = frame_filter(layout)
        if data.video_render_variant != 'raw':
            vf += f",ass=filename='{_subtitle_filter_path(ass)}'"
        output = root / 'frame.png'
        image_input = ['-loop', '1'] if shot.get('image') else []
        try:
            _run([ffmpeg_binary(), '-y', *image_input, '-i', str(source), '-vf', vf, '-frames:v', '1', str(output)], '布局预览', timeout=30)
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(422, '本地布局预览未能完成，请检查素材或稍后重试；布局设置仍然保留。') from exc
        return dict(image='data:image/png;base64,'+base64.b64encode(output.read_bytes()).decode(),
                    engine='动态视频 / FFmpeg', width=layout['width'], height=layout['height'])


def _run(command, label, timeout=None):
    completed = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, check=False, timeout=timeout)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or 'unknown error').strip()[-3000:]
        raise RuntimeError(f'{label}失败：{detail}')


def _end_title_filter(record, work, width, height, duration):
    """Explicit export option, within the last clip; no model reroll."""
    text = str((record.get('export_settings') or {}).get('end_title_text') or '').strip()
    if not text:
        return ''
    if len(text) > 80 or any(ord(c) < 32 for c in text):
        raise ValueError('片尾文字请使用不超过80字的单行文本')
    text = text.translate(str.maketrans({'\\': '＼', '{': '｛', '}': '｝'}))
    start = max(0, duration - 1.5)
    def stamp(seconds):
        ticks = round(seconds * 100)
        return f'{ticks//360000}:{ticks//6000%60:02d}:{ticks//100%60:02d}.{ticks%100:02d}'
    ass = Path(work) / 'end-title.ass'
    ass.write_text(
        '[Script Info]\nScriptType: v4.00+\n'
        f'PlayResX: {width}\nPlayResY: {height}\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
        f'Style: EndTitle,Microsoft YaHei,{round(height*.06)},&H00FFFFFF,&H00000000,0,1,0,0,5,40,40,0,1\n'
        '[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
        f'Dialogue: 0,{stamp(start)},{stamp(duration)},EndTitle,,0,0,0,,{text}\n', encoding='utf8')
    return (f",drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='gte(t,{start:.9f})',"
            f"subtitles=filename='{_subtitle_filter_path(ass)}':charenc=UTF-8")


def _size(record):
    return (1080, 1920) if record.get('settings', {}).get('ratio') == '9:16' else (1920, 1080)


def _safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '_', str(value or '动态视频')).strip(' .')
    return value[:80] or '动态视频'


def _asset(path: Path, relative: str) -> Path:
    result = (path / str(relative)).resolve()
    if path.resolve() not in result.parents:
        raise ValueError('动态视频资产路径无效')
    if not result.is_file() or result.stat().st_size <= 0:
        raise ValueError(f'动态视频资产不存在：{relative}')
    return result


def _source(path, shot):
    relative = shot.get('video') if shot.get('kind') == 'video' else shot.get('image')
    if not relative:
        raise ValueError(f'镜头 {shot.get("id")} 缺少{"视频" if shot.get("kind") == "video" else "图片"}资产')
    return _asset(path, relative)


def _has_audio_stream(path: Path) -> bool:
    completed = subprocess.run(
        [ffprobe_binary(), '-v', 'error', '-select_streams', 'a:0',
         '-show_entries', 'stream=index', '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True, encoding='utf-8', errors='replace', check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    return completed.returncode == 0 and bool((completed.stdout or '').strip())


def _ass_colour(value, default):
    match = re.fullmatch(r'#?([0-9a-fA-F]{6})', str(value or ''))
    if not match:
        return default
    rgb = match.group(1)
    return '&H00' + rgb[4:6] + rgb[2:4] + rgb[0:2]


def _subtitle_style(record, height):
    params = record.get('creation_parameters') or {}
    orientation = 'portrait' if record.get('settings', {}).get('ratio') == '9:16' else 'landscape'
    defaults = dict(font='Microsoft YaHei', size=56,
                    position=75 if orientation == 'portrait' else 95, color='#ffffff',
                    outline_color='#000000', outline=2, background=True, opacity=.75)
    defaults.update((params.get('subtitle_layouts') or {}).get(orientation) or {})
    font = str(defaults['font']).replace("'", '') or 'Microsoft YaHei'
    margin = max(10, round(height * max(0, 100-float(defaults['position'])) / 100))
    primary = _ass_colour(defaults['color'], '&H00FFFFFF')
    outline = _ass_colour(defaults['outline_color'], '&H00000000')
    border = 3 if defaults.get('background') else 1
    if border == 3:
        try:
            opacity = float(defaults['opacity'])
            opacity = max(0, min(1, opacity)) if math.isfinite(opacity) else .75
        except (ValueError, TypeError):
            opacity = .75
        # ASS opaque-box backgrounds use OutlineColour, including its alpha.
        outline = f'&H{round((1-opacity)*255):02X}' + outline[4:]
    return (f"FontName={font},FontSize={float(defaults['size']):g},Bold=1,Alignment=2,MarginV={margin},"
            f"PrimaryColour={primary},OutlineColour={outline},BackColour={outline},BorderStyle={border},"
            f"Outline={float(defaults['outline']):g},Shadow=0")


def _subtitle_ass(source, target, width, height):
    """Keep SRT text/timing while giving libass explicit output-pixel units."""
    _run([ffmpeg_binary(), '-y', '-sub_charenc', 'UTF-8', '-i', str(source), str(target)], '字幕样式准备')
    lines = target.read_text(encoding='utf-8-sig').splitlines()
    result, in_info, found_info = [], False, False
    for line in lines:
        if line.strip().startswith('['):
            in_info = line.strip().casefold() == '[script info]'
            if in_info:
                found_info = True
                result.extend((line, f'PlayResX: {width}', f'PlayResY: {height}', 'ScaledBorderAndShadow: yes'))
                continue
        if in_info and line.partition(':')[0].strip().casefold() in {
                'playresx', 'playresy', 'scaledborderandshadow'}:
            continue
        result.append(line)
    if not found_info:
        raise ValueError('字幕样式准备失败：ASS 缺少脚本信息')
    target.write_text('\n'.join(result) + '\n', encoding='utf-8')
    return target


def _timeline(record, *, narration_duration=None):
    """Allocate absolute subtitle time to clips, including silent frame holds.

    Quantize boundaries, not individual durations, so 30 fps rounding cannot
    accumulate across a long storyboard. Holds use the preceding last frame;
    before the first action they use the first frame of the first shot.
    """
    try:
        expected = float(record['scenes'][-1]['end'])
        shots = record['shots']
        boundaries = [(float(shot['start']), float(shot['end'])) for shot in shots]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError('成片缺少有效的字幕与镜头时间轴') from exc
    if not math.isfinite(expected) or expected <= 0 or not boundaries:
        raise ValueError('成片缺少有效的字幕与镜头时间轴')
    for index, (start, end) in enumerate(boundaries):
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start
                or end > expected + .001 or (index and start < boundaries[index-1][1] - .001)):
            raise ValueError(f'第 {index+1} 镜时间轴无效、重叠或超出字幕终点')
    if narration_duration is not None:
        narration_duration = float(narration_duration)
        if not math.isfinite(narration_duration) or narration_duration <= 0:
            raise ValueError('成片配音时长无效')
        # ASR subtitle endings can precede the final spoken sound. Extend only
        # the last frame hold, without moving subtitles or earlier cuts. Round
        # upward so frame quantization cannot shave off the narration tail.
        if narration_duration > expected:
            expected = math.ceil(narration_duration * 30) / 30
    segments = []
    for index, (start, end) in enumerate(boundaries):
        first_frame = round(start * 30) if index else 0
        next_frame = round((boundaries[index+1][0] if index+1 < len(boundaries) else expected) * 30)
        frames = next_frame - first_frame
        if frames <= 0:
            raise ValueError(f'第 {index+1} 镜时长不足一帧')
        segments.append(dict(frames=frames, duration=frames / 30,
                             lead=round(start * 30) / 30 if index == 0 else 0,
                             content_duration=end-start))
    return expected, segments


def _render(path, record):
    from .video_dialogue import native_mode
    if native_mode(record):
        from .video_native_export import render_native
        return render_native(path, record)
    path = path.resolve()
    expected, segments = _timeline(record)
    try:
        audio = _asset(path, record.get('audio') or '')
    except ValueError as exc:
        raise ValueError('成片缺少已确认的配音文件，请先检查或同步配音') from exc
    try:
        srt = _asset(path, 'assets/subtitles.srt')
    except ValueError as exc:
        raise ValueError('成片缺少已确认的字幕文件，请先检查或同步字幕') from exc
    if not _has_audio_stream(audio):
        raise ValueError('成片配音文件没有可读取的音轨，请先检查配音')
    expected, segments = _timeline(record, narration_duration=probe_media_duration(audio))
    sources = []
    for index, shot in enumerate(record['shots'], 1):
        if shot.get('kind') not in {'static', 'video'}:
            raise ValueError(f'第 {index} 镜类型无效')
        if shot['kind'] == 'video' and shot.get('video_status') != 'completed':
            raise ValueError(f'第 {index} 镜动态片段尚未完成')
        sources.append(_source(path, shot))
    width, height = _size(record)
    # Every attempt owns a new directory. Only the worker's successful commit
    # publishes it, so failed retries and same-name projects keep old exports.
    run_id = uuid.uuid4().hex
    output_root = PROJECT_ROOT / 'output' / (
        f'{_safe_name(record["settings"].get("name"))}_动态视频_'
        f'{_safe_name(path.parent.name)}_{_safe_name(path.name)}_{run_id}')
    work = path / 'export_work' / run_id
    work.mkdir(parents=True)
    output_root.mkdir(parents=True)
    clips = []
    use_video_audio = bool((record.get('export_settings') or {}).get('use_video_audio', True))
    layout = presentation(record)
    base_vf = frame_filter(layout)
    for index, (shot, source, segment) in enumerate(zip(record['shots'], sources, segments), 1):
        duration, lead = segment['duration'], segment['lead']
        content_duration = segment['content_duration']
        # PCM intermediates avoid AAC priming/padding at each concat boundary.
        clip = work / f'{index:04d}.mov'
        command = [ffmpeg_binary(), '-y']
        if shot['kind'] == 'static':
            command += ['-loop', '1', '-i', str(source)]
        else:
            command += ['-i', str(source)]
        stretch = 1.0
        if shot['kind'] == 'video':
            source_duration = _video_duration(source)
            # Only narration resync explicitly authorizes retiming. Ordinary
            # shots keep their performed timing and hold the final frame;
            # leading/trailing timeline gaps never contribute to the stretch.
            adjustment = shot.get('audio_timing_adjustment') or {}
            if adjustment.get('mode') == 'trim_or_slow' and content_duration > source_duration + 0.03:
                stretch = content_duration / source_duration
        keep_source_audio = bool(use_video_audio and shot['kind'] == 'video' and _has_audio_stream(source))
        audio_filter = ('aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,'
                        'asetpts=PTS-STARTPTS,')
        if keep_source_audio and stretch > 1:
            tempo = 1 / stretch
            while tempo < .5:
                audio_filter += 'atempo=0.5,'
                tempo /= .5
            audio_filter += f'atempo={tempo:.9f},'
        audio_filter += f'atrim=duration={content_duration:.9f},'
        if lead:
            audio_filter += f'adelay={round(lead * 48000)}S:all=1,'
        audio_filter += f'apad,atrim=end_sample={segment["frames"] * 1600}'
        if keep_source_audio:
            command += ['-map', '0:v:0', '-map', '0:a:0', '-af', audio_filter]
        else:
            command += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
                        '-map', '0:v:0', '-map', '1:a:0', '-af', audio_filter]
        vf = (f'setpts={stretch:.9f}*(PTS-STARTPTS),trim=duration={content_duration:.9f},fps=30,'
              f'tpad=start_mode=clone:start_duration={lead:.9f}:stop_mode=clone:stop_duration={duration:.9f},'
              f'trim=end_frame={segment["frames"]},setpts=N/(30*TB),{base_vf}')
        if index == len(sources):
            vf += _end_title_filter(record, work, width, height, duration)
        command += ['-t', f'{duration:.9f}', '-vf', vf, '-c:v', 'libx264', '-preset', 'fast',
                    '-crf', '18', '-pix_fmt', 'yuv420p', '-r', '30', '-c:a', 'pcm_s16le', str(clip)]
        _run(command, f'第 {index} 镜标准化')
        clips.append(clip)
    concat = work / 'concat.txt'
    concat.write_text('\n'.join("file '" + clip.as_posix().replace("'", "'\\''") + "'" for clip in clips), encoding='utf-8')
    assembled = work / 'assembled.mov'
    _run([ffmpeg_binary(), '-y', '-f', 'concat', '-safe', '0', '-i', str(concat), '-c', 'copy', str(assembled)], '镜头拼接')
    raw = output_root / '最终视频_纯净版.mp4'
    if use_video_audio:
        # Gains are authored explicitly above; amix's default normalization
        # would halve narration even when the padded effects track is silent.
        mix = ('[0:a:0]volume=0.65[effects];'
               f'[1:a:0]asetpts=PTS-STARTPTS,apad,atrim=duration={expected:.9f},volume={_narration_volume(record):g}[narration];'
               '[effects][narration]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,'
               'alimiter=limit=0.95:level=false[mixed]')
        audio_args = ['-filter_complex', mix, '-map', '0:v:0', '-map', '[mixed]']
    else:
        audio_args = ['-map', '0:v:0', '-map', '1:a:0', '-af',
                      f'asetpts=PTS-STARTPTS,apad,volume={_narration_volume(record):g}']
    _run([ffmpeg_binary(), '-y', '-i', str(assembled), '-i', str(audio), *audio_args,
          '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', f'{expected:.9f}',
          '-movflags', '+faststart', str(raw)], '配音与音效合成')
    shutil.copy2(srt, output_root / '最终字幕.srt')
    subtitled = output_root / '最终视频_字幕版.mp4'
    ass = write_ass(srt, work / 'subtitles.ass', layout)
    subtitle_filter = f"ass=filename='{_subtitle_filter_path(ass)}'"
    _run([ffmpeg_binary(), '-y', '-i', str(raw), '-vf', subtitle_filter, '-c:v', 'libx264', '-preset', 'fast',
          '-crf', '18', '-pix_fmt', 'yuv420p', '-c:a', 'copy', '-movflags', '+faststart', str(subtitled)], '字幕版合成')
    shutil.copy2(audio, output_root / '配音.wav')
    (output_root / '动态分镜方案.json').write_text(json.dumps(record['shots'], ensure_ascii=False, indent=2), encoding='utf-8')
    for output in (raw, subtitled):
        actual = probe_media_duration(output)
        if abs(actual - expected) > .1:
            raise RuntimeError(f'{output.name}时长校验失败：预期 {expected:.2f} 秒，实际 {actual:.2f} 秒')
    return output_root, raw, subtitled


def export_review_message(exported):
    review = exported.get('review') or {}
    if review.get('mode') == 'observation':
        return '观察版文件已生成，未执行导出前自动质量检查；尚未人工验收。'
    if review.get('mode') == 'automatic':
        return ('文件已生成；自动画面抽样检查 ' + str(review.get('visual_shots_checked', 0))
                + ' 镜、原声文字检查 ' + str(review.get('speech_shots_checked', 0))
                + ' 镜；声线、口型和完整观感尚需人工验收。')
    return '文件已生成；此历史导出没有质量检查记录，不能视为已验收。'


def reconcile_source_job(record, user_id: int) -> bool:
    """Close the original guided task after its dynamic-video child is exported.

    Dynamic video has an independent editable project, but users still see the
    original guided task in the project list. Both records must converge on the
    same terminal state. This helper is idempotent and also repairs projects
    exported by builds from before the lifecycle bridge existed.
    """
    source_id = str((record.get('source_project') or {}).get('id') or '').strip()
    exported = record.get('export') if isinstance(record.get('export'), dict) else {}
    if record.get('status') != 'completed' or not source_id or not exported:
        return False
    job = store.get(source_id)
    if job is None or job.user_id != int(user_id) or not job.request.get('dynamic_video'):
        return False
    project_id = str(record.get('id') or '').strip()
    # Cloned projects may retain source_project for provenance. Only the child
    # explicitly adopted by the guided task can update that task's lifecycle.
    if not project_id or str(job.request.get('_dynamic_video_project_id') or '').strip() != project_id:
        return False
    raw = Path(str(exported.get('raw') or '')).resolve()
    subtitled = Path(str(exported.get('subtitles') or '')).resolve()
    output_root = Path(str(exported.get('directory') or '')).resolve()
    if not raw.is_file() or not subtitled.is_file() or not output_root.is_dir():
        return False
    artifacts = dict(job.artifacts or {})
    artifacts.update(
        video_with_subtitles=f'/api/video-studio/{project_id}/export/subtitles',
        video_raw=f'/api/video-studio/{project_id}/export/raw',
    )
    previous_request = dict(job.request)
    if isinstance(exported.get('review'), dict):
        job.request['_dynamic_video_export_review'] = dict(exported['review'])
    else:
        job.request.pop('_dynamic_video_export_review', None)
    job.request['_dynamic_video_project_id'] = project_id
    # Keep the guided task's narration workspace stable.  Replacing this with
    # the final-video folder made later sentence-level TTS edits write into an
    # export directory that has no editable subtitle timeline.
    job.request['_dynamic_video_output_dir'] = output_root.name
    job.request['_step_mode_stage'] = 'completed'
    already_synced = (
        job.status == 'completed'
        and str(job.request.get('_step_mode_stage') or '') == 'completed'
        and job.artifacts.get('video_raw') == artifacts['video_raw']
        and job.artifacts.get('video_with_subtitles') == artifacts['video_with_subtitles']
        and previous_request == job.request
    )
    if not already_synced:
        store.update(job, request=job.request, status='completed', step='completed', progress=100,
                     message=export_review_message(exported), error=None,
                     artifacts=artifacts)
        persist_step_workflow_state(job, 'completed', message=job.message)
        store.update(job, request=job.request)
        store.log(job, f'动态视频项目 {project_id} 已完成，字幕版与纯净版已归入本任务。')
        # Register actual files for project history and support diagnostics.
        register_job_asset(job, subtitled, 'project_output',
                           {'dynamic_video': True, 'variant': 'subtitles', 'project_id': project_id})
        register_job_asset(job, raw, 'project_output',
                           {'dynamic_video': True, 'variant': 'raw', 'project_id': project_id})
    return True


def reconcile_completed_projects() -> int:
    """Repair lifecycle links for exports produced by older OCV builds."""
    repaired = 0
    if not studio.ROOT.is_dir():
        return repaired
    for user_dir in studio.ROOT.iterdir():
        if not user_dir.is_dir() or not user_dir.name.isdigit():
            continue
        for record_path in user_dir.glob('*/record.json'):
            try:
                record = json.loads(record_path.read_text(encoding='utf-8'))
                if reconcile_source_job(record, int(user_dir.name)):
                    repaired += 1
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    return repaired


def _worker(path, user_id, review_generated=False):
    try:
        with studio.LOCK:
            record = studio.read(path)
        visual_reviews, speech_reviews = [], []
        if review_generated:
            from .video_quality import check_automatic_export
            def review_progress(message):
                with studio.LOCK:
                    current=studio.read(path)
                    current['logs'].append(message)
                    studio.save(path,current)
            visual_reviews = check_automatic_export(path, record, user_id, progress=review_progress)
            from .video_dialogue import native_mode
            if native_mode(record):
                from .video_dialogue_quality import check_native_speech
                speech_reviews = check_native_speech(path, record, progress=review_progress)
                record['_native_speech_reviews'] = speech_reviews
        # Internal worker intent, never a client-supplied approval flag.
        record['_native_observation_export'] = not review_generated
        output_root, raw, subtitled = _render(path, record)
        review = dict(mode='automatic' if review_generated else 'observation',
                      visual_shots_checked=len(visual_reviews), speech_shots_checked=len(speech_reviews),
                      human_acceptance='pending', source_revision=record['revision'])
        with studio.LOCK:
            current = studio.read(path)
            current.update(status='completed', error='', revision=current['revision']+1,
                           export=dict(directory=str(output_root), raw=str(raw), subtitles=str(subtitled),
                                       review=review))
            from .video_dialogue import native_mode
            from .video_external_dialogue import enabled as exact_enabled
            current['logs'].append('TTS对白合成完成：字幕使用实测配音位置，原H3片段保留归档，口型仍需观看核对。' if exact_enabled(current) else '原生对话合成完成：已保留 H3 原声，输出字幕版、纯净版、SRT 与分镜方案。'
                + ('字幕按原声识别时间对齐，声线与口型仍需观看核对。' if review_generated else '这是观察版，字幕采用计划时间，尚未通过原声检查。')
                if native_mode(current) else '动态视频合成完成：已输出字幕版、纯净版、SRT、配音与分镜方案。')
            studio.save(path, current)
            finished = current.copy()
        try:
            reconcile_source_job(finished, user_id)
        except Exception as exc:
            # The dynamic result is valid even if bookkeeping is temporarily
            # unavailable. A later project GET/list call retries idempotently.
            with studio.LOCK:
                current = studio.read(path)
                current['logs'].append('来源任务状态暂未同步，将在下次打开项目时自动重试：' + str(exc)[:1200])
                studio.save(path, current)
    except Exception as exc:
        with studio.LOCK:
            current = studio.read(path)
            current.update(status='export_failed', error=str(exc)[:3000], revision=current['revision']+1)
            current['logs'].append('动态视频合成失败：' + current['error'])
            studio.save(path, current)
    finally:
        studio.ACTIVE.discard(str(path))


@router.post('/{identity}/export')
def start_export(identity: str, data: ExportRequest, request: Request):
    return start_export_for_user(identity, data, studio.require_user(request))


def start_export_for_user(identity: str, data: ExportRequest, user: dict, *, review_generated=False):
    with studio.LOCK:
        user_id = int(user['id'])
        path = studio.directory(user_id, identity)
        record = studio.read(path)
        studio.editable(record, data.revision)
        if record.get('video_waiting_queue') or any(shot.get('video_queued') or shot.get('video_waiting')
                                                   for shot in record.get('shots', [])):
            raise HTTPException(409, '请先完成或取消生成队列，再合成成片')
        all_static_ready = (bool(record.get('shots')) and
                            all(shot.get('kind') == 'static' and shot.get('image_status') == 'completed'
                                for shot in record['shots']))
        if record['status'] not in {'video_review', 'video_generation_ready', 'export_failed', 'completed'} and not (
                record['status'] == 'video_generation_ready' and all_static_ready):
            raise HTTPException(409, '请先完成全部动态镜头')
        if not record.get('shots'):
            raise HTTPException(409, '项目没有可合成的镜头')
        missing = [shot['id'] for shot in record['shots'] if shot['kind']=='video' and shot.get('video_status')!='completed']
        if missing:
            raise HTTPException(409, f'仍有 {len(missing)} 个动态镜头未完成')
        from .video_core_references import dependency_error
        for shot in record['shots']:
            problem = dependency_error(path, record, shot)
            if problem:
                raise HTTPException(409, f'{shot["id"]}：{problem}；请更新对应镜头后再导出')
        if str(path) in studio.ACTIVE:
            raise HTTPException(409, '此任务正在处理')
        from .video_dialogue import native_mode, assert_native_export_current
        native = native_mode(record)
        legacy=[]
        if native:
            if not data.use_video_audio:
                raise HTTPException(400, '原生对话不能关闭原声；角色台词就在 H3 的音轨中')
            try:
                legacy=assert_native_export_current(user_id, path, record, observation_only=not review_generated)
            except (OSError, ValueError) as exc:
                raise HTTPException(409, str(exc)) from exc
        narration_volume = data.narration_volume if data.narration_volume is not None else _narration_volume(record)
        end_title_text = (data.end_title_text if data.end_title_text is not None else
                          (record.get('export_settings') or {}).get('end_title_text', '')).strip()
        if any(ord(c) < 32 for c in end_title_text):
            raise HTTPException(400, '片尾文字请使用单行文本')
        studio.ACTIVE.add(str(path))
        if data.subtitle_layouts is not None:
            record.setdefault('creation_parameters', {})['subtitle_layouts'] = data.subtitle_layouts
        record.update(status='exporting', error='', revision=record['revision']+1,
                      export_settings={'use_video_audio': data.use_video_audio, 'narration_volume': narration_volume,
                                       'end_title_text': end_title_text})
        if end_title_text:
            record['logs'].append('片尾文字将在最后镜头末尾1.5秒内合成，不改变总时长、配音或原始片段。')
        record['logs'].append('开始合成原生对话：按每镜实际长度保留原声拼接，不用旁白时间轴裁切句尾。' if native else
                              '开始合成动态视频：按字幕时长裁切动态片段并补齐静态镜头；' +
                              ('保留动态片段音效并与 TTS 配音混合。' if data.use_video_audio else '忽略动态片段原声，仅使用 TTS 配音。') +
                              f'旁白音量 {narration_volume:.0%}。')
        if legacy:
            record['logs'].append('观察版保留旧参考音频规则生成的镜头：'+', '.join(legacy)
                +'。角色、台词、图片与所选音色未变；旧片不算当前音频规则验收通过，自动一键仍需重做。')
        studio.save(path, record)
        threading.Thread(target=_worker, args=(path, user_id, review_generated), daemon=True).start()
        return record


@router.get('/{identity}/export/{variant}')
def download(identity: str, variant: str, request: Request):
    path = studio.directory(studio.require_user(request)['id'], identity)
    record = studio.read(path)
    if variant not in {'raw', 'subtitles'} or not isinstance(record.get('export'), dict):
        raise HTTPException(404, '成片尚未生成')
    file = Path(record['export'][variant]).resolve()
    root = (PROJECT_ROOT / 'output').resolve()
    if root not in file.parents or not file.is_file():
        raise HTTPException(404, '成片文件不存在')
    return FileResponse(file, media_type='video/mp4', filename=file.name, content_disposition_type='inline')


@router.post('/{identity}/export/open')
def open_folder(identity: str, request: Request):
    path = studio.directory(studio.require_user(request)['id'], identity)
    record = studio.read(path)
    folder = Path((record.get('export') or {}).get('directory', '')).resolve()
    root = (PROJECT_ROOT / 'output').resolve()
    if root not in folder.parents or not folder.is_dir():
        raise HTTPException(404, '输出目录尚未生成')
    if os.name == 'nt':
        os.startfile(folder)  # type: ignore[attr-defined]
    return {'ok': True}
