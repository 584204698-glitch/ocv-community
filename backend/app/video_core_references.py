"""Resolve checked opening/prop-appearance references and saved dependencies."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .video_core_reference_plan import appearance_signature, plan_core_reference_groups, reference_signature
from .video_native_image_quality import screen_group

POLICY = 1
MARKER = '\n【核心连续性参考】'


def strip_hint(prompt):
    return str(prompt or '').split(MARKER, 1)[0].strip()


def _edge(record, shot):
    plan = plan_core_reference_groups(record)
    return next((row for row in plan if row['shot_id'] == shot['id']), None)


def _bound_edge(record, shot, used):
    """Validate saved source directly; a new fallback must not rewrite lineage."""
    if not record.get('native_story'):
        raise ValueError('连续性引用的项目依据已变化')
    from .video_scene_references import enabled
    if not enabled(record):
        raise ValueError('连续性引用的场景依据已变化')
    shots = record.get('shots', [])
    target_index = next((i for i, row in enumerate(shots) if row['id'] == shot['id']), -1)
    source = next((row for row in shots[:max(target_index, 0)] if row['id'] == used.get('source_shot_id')), None)
    mode = used.get('mode', 'opening')
    if source is None or mode not in {'opening', 'appearance_only'}:
        raise ValueError('连续性引用的来源或模式无效')
    if mode == 'opening':
        signature = reference_signature(record, shot)
        if signature != reference_signature(record, source):
            signature = None
    else:
        signature = appearance_signature(record, shot, source)
    if not signature or signature != used.get('signature'):
        raise ValueError('连续性引用的角色、场景或道具开场依据已变化')
    return dict(shot_id=shot['id'], source_shot_id=source['id'], signature=signature, mode=mode)


def _source(path, record, edge):
    from .video_studio import _storyboard_image_path, _stale_scene_reference
    source = next((row for row in record['shots'] if row['id'] == edge['source_shot_id']), None)
    if not source or source.get('image_status') != 'completed':
        raise ValueError('连续性源图尚未完成，未提交依赖镜头')
    if ((source.get('image_task') or {}).get('status') in {'running', 'failed'}
            or source.get('image_error') or source.get('image_prompt_out_of_sync') or source.get('design_needs_review')):
        raise ValueError('连续性源图有未解决的编辑或设计，未提交依赖镜头')
    if source.get('core_reference_used'):
        raise ValueError('连续性组首图仍继承旧依赖，须先核对，不建立递归引用')
    if _stale_scene_reference(record, source):
        raise ValueError('连续性源图仍使用旧场景版本，未提交依赖镜头')
    file = _storyboard_image_path(Path(path), source)
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    if digest != source.get('image_version'):
        raise ValueError('连续性源图实际文件与保存版本不符')
    return source, file, digest


def resolve_reference(path, record, shot, *, review=False, ask=None):
    """No paid check unless the existing image worker explicitly permits one."""
    if record.get('core_reference_policy') != POLICY:
        return None
    edge = _edge(record, shot)
    if not edge or not edge['source_shot_id']:
        return None
    source, file, digest = _source(path, record, edge)
    from .video_review_policy import strict_quality_review
    if not strict_quality_review(record):
        return dict(file=str(file.resolve()), binding=dict(version=POLICY,
            source_shot_id=source['id'], source_image_sha256=digest,
            signature=edge['signature'], mode=edge['mode'], review_status='not_requested'))
    report = screen_group(path, [source], cached_only=not review, ask=ask)
    if report is None:
        raise ValueError('连续性源图尚无当前版本的实图检查，未提交依赖镜头')
    result = report['review']
    if result['issues'] or result['uncertainties']:
        raise ValueError('连续性源图检查有缺陷或待核实项，已保留原图与报告，未提交依赖镜头')
    if report['image_versions'].get(source['id']) != digest:
        raise ValueError('连续性源图检查版本不一致')
    return dict(file=str(file.resolve()), binding=dict(version=POLICY,
        source_shot_id=source['id'], source_image_sha256=digest,
        signature=edge['signature'], mode=edge['mode'], review_fingerprint=report['fingerprint']))


def dependency_error(path, record, shot):
    """Check actual saved lineage without model calls or altering completed work."""
    used = shot.get('core_reference_used')
    if not used:
        return ''
    try:
        if not isinstance(used, dict) or used.get('version') != POLICY:
            raise ValueError('连续性引用记录版本无效')
        edge = _bound_edge(record, shot, used)
        _, _, digest = _source(path, record, edge)
        if digest != used.get('source_image_sha256'):
            raise ValueError('连续性源图已换版，本镜仍引用旧版本')
        if not used.get('review_fingerprint') and used.get('review_status') != 'not_requested':
            raise ValueError('连续性引用缺少检查依据')
    except (ValueError, OSError) as exc:
        return str(exc)
    except Exception as exc:
        # Missing source files use the application's HTTP error type; preserve
        # this as an unresolved dependency instead of treating the image valid.
        from fastapi import HTTPException
        if isinstance(exc, HTTPException):
            return '连续性源图文件缺失，尚未核实本镜引用'
        raise
    return ''


def reference_prompt(prompt, number, *, mode='opening'):
    if mode == 'appearance_only':
        return strip_hint(prompt) + (f'{MARKER}图{number}是同场景的道具外观参考（appearance_only），不代表质量已经通过。'
            '只锁定本镜明确要求开场可见且具有相同稳定道具ID和外观定义的道具外观、结构、形状、颜色、材质及固定环境。'
            '当前批准的开场状态与镜头要求优先：不复制参考图中的旧损坏、修补、开关、位置、占用或遮挡状态。'
            '修补仅按本镜批准动作与状态表现，不改变固定外观定义，不自行新增花纹、图案或颜色。'
            '不复制参考图演员或人数，不新增人物；人物身份以本镜cast和各自角色参考图为准。'
            '完整执行本镜批准动作与台词，按本镜景别和取景重新构图，不复制参考图机位、人物大小、姿态或表情。'
            '不提前画后续动作或结果，不让稍后出现或画外的道具提前入画；本镜要求可见的道具与接触部位须保留。')
    if mode != 'opening':
        raise ValueError('连续性引用模式无效')
    return strip_hint(prompt) + (f'{MARKER}图{number}是同场景同开场状态核心图，仅用作连续性参考，不代表质量已经通过。'
        '只沿用其中空间连接、关键道具外观和没有剧情变化的相对位置；'
        '人物身份仍以各自角色参考图为准。当前镜头的批准景别、取景、表情及动作优先，'
        '按当前镜头重新取景，不照搬参考图机位、人物大小或姿势。'
        '连续性约束的是场景内物件的存在、外观和空间关系，不要求参考图中的所有内容同时入画。'
        '允许批准取景之外的身体、家具及装饰自然出画；出画不等于物件被删除或移位。'
        '本镜明确要求可见的角色、道具、接触部位和地标必须保留在画内，不能借裁切漏掉剧情依据；'
        '画内仍可见的物件保持外观与空间关系。不提前执行后续动作，不凭空增添人物或物件。')


def refresh_notices(path, record):
    """Derived UI messages only; request gates always verify real dependencies."""
    for shot in record.get('shots', []):
        if shot.get('core_reference_used'):
            shot['core_reference_error'] = dependency_error(path, record, shot)
        else:
            shot.pop('core_reference_error', None)
