"""User-selected visual rhythm, independent of narration file boundaries."""
import copy
import math

from .gemini_client import GeminiError
from .video_group_repair import (_decorate_children, _duration, _fallback_row,
                                 _validate_children, validate_group_structure)

PACING_FIELDS = ('visual_pacing_preset', 'visual_min_duration', 'visual_target_duration',
                 'visual_max_duration', 'visual_max_slides')
PRESETS = {'slow': (6, 10, 15), 'standard': (5, 8, 12), 'fast': (4, 6, 8)}


def pacing_policy(parameters):
    preset = parameters.get('visual_pacing_preset', 'auto')
    if preset not in (*PRESETS, 'custom'):
        return None  # Missing/auto preserves legacy semantic grouping.
    minimum, target, maximum = PRESETS.get(preset, PRESETS['standard'])
    def bounded(key, default, low, high):
        try:
            value = float(parameters.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value if math.isfinite(value) else default))
    max_slides = None
    if preset == 'custom':
        minimum = bounded('visual_min_duration', minimum, 4, 15)
        target = bounded('visual_target_duration', target, max(5, minimum), 15)
        maximum = bounded('visual_max_duration', maximum, max(6, target), 15)
        max_slides = int(bounded('visual_max_slides', 6, 1, 12))
    return dict(preset=preset, min_duration=minimum, target_duration=target,
                max_duration=maximum, max_slides=max_slides)


PACING_CONTRACT = """
用户已选择镜头节奏，visual_pacing 是实际分镜约束。以 target_duration 为目标，
在角色反应、动作变化、发现、转折等可见节拍处切镜；同一段旁白可以连续覆盖多个镜头。
完整语义可以通过相邻镜头连续表达，不等于必须留在同一个画面中；保留因果、指代和动作衔接。
多条字幕组成的镜头不得超过 max_duration；若 max_slides 非空，也不得超过该字幕数。
min_duration 是避免闪切的偏好，不是硬下限；不得为凑秒数改配音、改字幕或编造新剧情。
单条字幕无法再切时可独立超过节奏上限，超过15秒仍按既有规则使用静态画面。
不要把动态改成静态来绕过节奏要求。使用原字幕边界，配音文件数不限制镜头数。
"""


def exceeds(local, policy):
    return len(local) > 1 and (_duration(local) > policy['max_duration'] + .001
                             or (policy['max_slides'] and len(local) > policy['max_slides']))


def pacing_partitions(scenes, policy):
    """Bounded fallback favors target duration and complete punctuation, without new times."""
    count = len(scenes)
    best, following = [float('inf')] * (count + 1), [count] * count
    best[count] = 0
    for first in range(count - 1, -1, -1):
        for end in range(first + 1, count + 1):
            local = scenes[first:end]
            if exceeds(local, policy):
                break
            seconds = _duration(local)
            ending = str(local[-1].get('text', '')).rstrip().rstrip('”’」』）)]】')
            weak = end < count and ending.endswith(('，', ',', '：', ':', '、', '；', ';'))
            cost = best[end] + (seconds - policy['target_duration']) ** 2
            cost += max(0, policy['min_duration'] - seconds) ** 2 * 4 + int(weak) * 9
            if cost < best[first]:
                best[first], following[first] = cost, end
    result, first = [], 0
    while first < count:
        end = following[first]
        result.append(scenes[first:end])
        first = end
    return result


def enforce_pacing(context, scenes, rows, policy, ask, *, progress=None, on_draft=None):
    """One bounded semantic request only if the normal directors ignored the selected rhythm."""
    if not policy:
        return rows
    validate_group_structure(rows, scenes)
    by_id = {s['slide_id']: s for s in scenes}
    offenders = [row for row in rows if not row.get('manual_locked')
                 and exceeds([by_id[sid] for sid in row['slide_ids']], policy)]
    if not offenders:
        return rows
    if progress:
        progress(f'镜头节奏：按所选节奏细分 {len(offenders)} 个长镜头，配音保持不变。')
    payload = dict(story_context=context, source_text='\n'.join(s['text'] for s in scenes),
                   visual_pacing=policy, groups=[dict(parent_id=row['id'], kind=row['kind'],
                   subtitles=[by_id[sid] for sid in row['slide_ids']],
                   original_intent=row.get('intent', '')) for row in offenders])
    system = """你是镜头节奏协调员。只拆分 groups 中指定镜头，每组独立覆盖自己的全部字幕，顺序不变。
根据全文理解每次动作和反应，为子镜分别填写 intent、semantic、motion_basis、progression_plan。
动态父镜的子镜仍为动态，静态父镜仍为静态。单条字幕超过15秒的例外使用静态。
每个子镜的目的和原文依据只描述自己覆盖的内容，不能重复父镜的全部动作。
semantic 包含 message、source_basis、fact_status、progression、continuity_requirement。
fact_status 只能是 fact/hypothetical/metaphorical/quoted/question。保持原文的事实属性。
返回 {groups:[{parent_id,shots:[{slide_ids:[],kind,intent,semantic,motion_basis,progression_plan}]}]}。
""" + PACING_CONTRACT
    candidates = {}
    try:
        response = ask(system, payload)
        groups = response.get('groups') if isinstance(response, dict) else None
        if not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups):
            raise ValueError('节奏协调未返回分组')
        ids = [g.get('parent_id') for g in groups]
        if ids != [row['id'] for row in offenders]:
            raise ValueError('节奏协调镜头缺失、重复或顺序改变')
        candidates = {g['parent_id']: g.get('shots') for g in groups}
    except (GeminiError, ValueError, TypeError):
        if progress:
            progress('节奏协调暂未返回有效分组，将按现有字幕边界完成拆分。')
    output = []
    for parent in rows:
        if parent not in offenders:
            output.append(copy.deepcopy(parent))
            continue
        local = [by_id[sid] for sid in parent['slide_ids']]
        dynamic = parent['kind'] == 'video'
        method = 'semantic'
        try:
            children = _validate_children({'shots': candidates.get(parent['id'])}, local, dynamic)
            if any(exceeds([by_id[sid] for sid in row['slide_ids']], policy) for row in children):
                raise ValueError('拆分后仍超过所选节奏')
        except (ValueError, TypeError):
            method = 'boundary_fallback'
            children = [_fallback_row(part, parent, dynamic) for part in pacing_partitions(local, policy)]
        output.extend(_decorate_children(children, parent, by_id, method, '按用户选择的镜头节奏细分'))
    validate_group_structure(output, scenes)
    if on_draft:
        on_draft(copy.deepcopy(output))
    if progress:
        progress(f'镜头节奏已落实：{len(rows)} 镜调整为 {len(output)} 镜；配音与字幕时间不变。')
    return output
