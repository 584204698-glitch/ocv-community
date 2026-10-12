"""Per-utterance post-dub positioning on an immutable video; no inference."""
import copy
import hashlib
import math

from .video_external_narration import digest


def basis(source, data):
    return digest(dict(video_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                       clips=data['clips'], key=data['key']))


def place(clips, starts, duration):
    if len(starts) != len(clips):
        raise ValueError('音频条数量与本镜配音不一致，请刷新后重试')
    result = copy.deepcopy(clips)
    for clip, start in zip(result, starts):
        length = float(clip['duration'])
        if (isinstance(start, bool) or not isinstance(start, (int, float))
                or not math.isfinite(start) or start < 0
                or not math.isfinite(length) or length <= 0 or start + length > duration + .002):
            raise ValueError('配音必须完整位于本镜范围内；不会截断或加速声音')
        clip.update(start=float(start), end=float(start) + length)
    return result


def effective_clips(shot, source, data, duration):
    timing = shot.get('manual_dub_timing')
    if not timing:
        automatic=shot.get('dialogue_alignment') or {}
        if automatic.get('audio_starts') is not None:
            if automatic.get('audio_basis')!=basis(source,data):
                raise ValueError('本镜素材已变化，旧自动配音位置不再适用，请重新校准')
            return place(data['clips'],automatic['audio_starts'],duration)
        return copy.deepcopy(data['clips'])
    if timing.get('basis') != basis(source, data):
        raise ValueError('本镜视频或配音已变化，旧手动时序不再适用，请重新调整或恢复自动时序')
    return place(data['clips'], timing['starts'], duration)
