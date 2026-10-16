"""Reference capacity belongs to a provider profile, not a redraw widget.

Sixteen is this application's configurable ceiling, not a provider guarantee.
Existing and cloud-pool profiles retain their previous four-image total.
"""
DEFAULT_REFERENCE_IMAGES = 4
MAX_REFERENCE_IMAGES = 16


def reference_limit(config):
    if config.get('reference_images') is False:
        return 0
    value = config.get('max_reference_images', DEFAULT_REFERENCE_IMAGES)
    if type(value) is not int or not 1 <= value <= MAX_REFERENCE_IMAGES:
        raise ValueError('参考图总上限须为 1–16 的整数，请核对图像配置')
    return value


def profile_reference_limit(snapshot, current):
    # Capacity updates apply to existing projects using the SAME model. An
    # edited profile must never transfer another model's allowance to a snapshot.
    same_model = snapshot.get('model_id', current.get('model_id')) == current.get('model_id')
    config = dict(snapshot)
    if same_model:
        config['max_reference_images'] = current.get('max_reference_images', DEFAULT_REFERENCE_IMAGES)
    if current.get('reference_images') is False:
        config['reference_images'] = False
    return reference_limit(config)


def project_reference_limit(record):
    parameters = record.get('creation_parameters', {})
    if parameters.get('use_cloud_image_pool'):
        return DEFAULT_REFERENCE_IMAGES
    snapshot = parameters.get('image_profile_snapshot') or {}
    identity = snapshot.get('id') or parameters.get('image_profile_id')
    if identity:
        from .image_profiles import list_profiles
        current = next((p for p in list_profiles() if p.get('id') == identity), None)
        if current:
            return profile_reference_limit(snapshot, current)
    return reference_limit(snapshot)


def validate_reference_count(count, config):
    limit = reference_limit(config)
    if count > limit:
        raise ValueError(f'本次共需 {count} 张参考图，所选图像配置总上限为 {limit} 张（含当前画面和场景）；未提交生成，请取消部分参考或调整图像配置')
