"""Deterministic H3 timbre excerpts; never modify generated video soundtracks.

Official Ref2VA limits are 2--15 s per audio and at most 15 s combined.
A fixed excerpt of at most five seconds per saved voice keeps conditioning bytes
stable regardless of which other speakers appear in a shot (up to three).
Prefer a detected quiet boundary rather than cutting a reference mid-utterance.
The adopted Qwen original remains unchanged and its hash is recorded separately.
"""
from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path
import wave

POLICY = 'qwen-pcm-quiet-boundary-5s-v2'
MAX_SECONDS = 5


def _pcm(data):
    try:
        reader = wave.open(io.BytesIO(data), 'rb')
        params = reader.getparams()
        frames = reader.readframes(params.nframes)
        reader.close()
    except (wave.Error, EOFError) as exc:
        raise ValueError('H3 音色参考必须是有效的 PCM WAV，请重新设计或采用音色') from exc
    if (params.comptype != 'NONE' or params.nchannels not in (1, 2)
            or params.sampwidth not in (1, 2, 3, 4) or params.framerate <= 0
            or len(frames) != params.nframes * params.nchannels * params.sampwidth):
        raise ValueError('H3 音色参考 WAV 数据不完整或格式不支持')
    return params, frames


def _checked_source(source, expected_sha256):
    data = Path(source).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('角色音色在校验期间发生变化，请重新核对')
    params, frames = _pcm(data)
    duration = params.nframes / params.framerate
    if duration < 2:
        raise ValueError('H3 参考音色至少需要 2 秒，请重新设计并采用音色')
    return data, params, frames


def _excerpt_count(params, frames):
    """Bounded PCM-only boundary selection; no ASR, LLM or resampling.

    Quiet is not proof of a linguistic sentence boundary. If there is no clear
    pause, preserve the old bounded excerpt rather than inventing a cutoff.
    The decision depends on this voice alone, never the current cast or story.
    """
    limit = min(params.nframes, MAX_SECONDS * params.framerate)
    if limit == params.nframes:
        return limit, 'complete_source'
    width, channels = params.sampwidth, params.nchannels
    stride = width * channels
    window = max(1, round(params.framerate * .02))
    levels = []
    full_scale = float(1 << (8 * width - 1))
    for start in range(0, limit, window):
        end = min(start + window, limit)
        block = frames[start * stride:end * stride]
        values = [(sample - 128) if width == 1 else sample
                  for sample in (int.from_bytes(block[i:i + width], 'little', signed=width != 1)
                                 for i in range(0, len(block), width))]
        levels.append(math.sqrt(sum(v * v for v in values) / len(values)) / full_scale)
    peak = max(levels, default=0)
    threshold = max(.002, min(.015, peak * .025))
    # Do not shorten a quiet source, or one already ending in a clear pause.
    if peak < threshold * 4 or all(value <= threshold for value in levels[-6:]):
        return limit, 'bounded_end_quiet'
    candidates = []
    run_start = None
    for index, level in enumerate(levels + [peak]):
        if level <= threshold:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            if index - run_start >= 6:
                boundary = (run_start + (index - run_start) // 2) * window
                if boundary >= 2 * params.framerate:
                    candidates.append(boundary)
            run_start = None
    if candidates:
        return candidates[-1], 'quiet_boundary'
    return limit, 'bounded_no_clear_pause'


def inspect_voice_reference(source, expected_sha256):
    """Read-only adoption preflight using the exact same rules as submission.

    Long originals are valid: the existing freeze policy selects five seconds.
    Compatibility is not a verdict about pronunciation or timbre quality.
    """
    _, params, frames = _checked_source(source, expected_sha256)
    duration = params.nframes / params.framerate
    count, boundary = _excerpt_count(params, frames)
    return {'source_duration': duration, 'duration': count / params.framerate,
            'policy': POLICY, 'boundary': boundary}


def freeze_voice_reference(source, destination, expected_sha256):
    data, params, frames = _checked_source(source, expected_sha256)
    duration = params.nframes / params.framerate
    count, boundary = _excerpt_count(params, frames)
    if count == params.nframes:
        output = data
    else:
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as writer:
            writer.setparams(params)
            writer.writeframes(frames[:count * params.nchannels * params.sampwidth])
        output = buffer.getvalue()
    Path(destination).write_bytes(output)
    return {'sha256': hashlib.sha256(output).hexdigest(),
            'source_sha256': expected_sha256, 'source_duration': duration,
            'duration': count / params.framerate, 'start': 0, 'policy': POLICY,
            'boundary': boundary}


def validate_voice_references(paths):
    if len(paths) > 3:
        raise ValueError('H3 每镜最多采用 3 段参考音色')
    durations = []
    for path in paths:
        params, _ = _pcm(Path(path).read_bytes())
        seconds = params.nframes / params.framerate
        if not 2 <= seconds <= 15:
            raise ValueError('H3 每段参考音色必须为 2–15 秒，未上传或提交')
        durations.append(seconds)
    if sum(durations) > 15:
        raise ValueError('H3 本镜所有参考音色合计不能超过 15 秒，未上传或提交')
    return durations
