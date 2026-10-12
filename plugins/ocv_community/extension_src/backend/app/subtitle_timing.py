"""Validate spoken-caption times against measured audio, not ASR predictions."""
import math
import wave
from pathlib import Path


def wav_duration(path):
    with wave.open(str(path), 'rb') as stream:
        duration = stream.getnframes() / stream.getframerate()
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('配音文件没有有效时长')
    return duration


def bound_subtitle_times(rows, duration, *, repair_final_end=True):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('配音时长无效')
    result = [dict(row) for row in rows]
    previous_end = 0.0
    for index, row in enumerate(result):
        start, end = float(row['start']), float(row['end'])
        if not all(math.isfinite(value) for value in (start, end)) or start < 0 or end <= start:
            raise ValueError(f'第 {index+1} 条字幕起止时间无效')
        if start < previous_end - .06 or start >= duration:
            raise ValueError(f'第 {index+1} 条字幕时间重叠或已超过配音时长，请重新校对')
        if end > duration + .005:
            # A valid final cue can have a hallucinated ASR end timestamp.
            # Only clip its overhang; never stretch earlier text or discard it.
            if not repair_final_end or index != len(result)-1:
                raise ValueError(f'第 {index+1} 条字幕结束于 {end:.2f} 秒，超过实际配音 {duration:.2f} 秒')
            end = duration
        row.update(start=start, end=round(min(end, duration), 3))
        if row['end'] <= start:
            raise ValueError('字幕在配音结束处没有有效显示时长')
        previous_end = row['end']
    return result
