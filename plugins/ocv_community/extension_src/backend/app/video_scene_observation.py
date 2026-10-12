"""Reusable pixel observations without story counts or desired appearance."""
import base64
import hashlib
import io
import json
import math
from pathlib import Path
from PIL import Image

SYSTEM = '''只观察提供的场景图片，不推测故事、理想画风或应该存在的物体数量。
逐件列出主要家具、门窗、可读标牌文字和明显人物；同类多个独立物体分别列出。
每件物体给出可见位置、可确认的主体/部件和0到1000归一化box=[left,top,right,bottom]。
区分独立物体与同一物体的部件，不因椅腿或桌腿多而重复计数，不补造遮住的物体。
可见文字逐字抄写到text，不翻译；看不清写入uncertainties。无需逐个列出叶子或装饰花瓣。
appearance只描述色块、轮廓、纹理和光影的实际表现，不判断是否合格。
返回JSON {objects:[{kind,description,box,text}],appearance,uncertainties:[]}。
每项description最多300字，text没有则为空串，appearance最多800字。'''


def validate_observation(value):
    if not isinstance(value, dict) or not isinstance(value.get('objects'), list) or len(value['objects']) > 40:
        raise ValueError('场景观察缺少逐件可见物体，未自动确认')
    for item in value['objects']:
        if (not isinstance(item, dict) or not isinstance(item.get('kind'), str)
                or not 1 <= len(item['kind']) <= 64 or not isinstance(item.get('description'), str)
                or not 1 <= len(item['description']) <= 500 or not isinstance(item.get('text'), str)
                or len(item['text']) > 400):
            raise ValueError('场景观察缺少物体的实际描述')
        box = item.get('box')
        if (not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1000 for v in box)
                or box[0] >= box[2] or box[1] >= box[3]):
            raise ValueError('场景观察的物体位置无效')
    if not isinstance(value.get('appearance'), str) or not 1 <= len(value['appearance']) <= 1200:
        raise ValueError('场景观察缺少实际画面表现')
    uncertain = value.get('uncertainties')
    if not isinstance(uncertain, list) or len(uncertain) > 20 or any(
            not isinstance(v, str) or len(v) > 800 for v in uncertain):
        raise ValueError('场景观察不确定项格式无效')
    return value


def observe_scene(path, asset, *, ask=None, cached_only=False):
    from .video_studio import _storyboard_image_path
    data = _storyboard_image_path(Path(path), asset).read_bytes()
    identity = hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256((SYSTEM + identity).encode()).hexdigest()
    target = Path(path) / 'assets/scene_observations' / (digest + '.json')
    if target.is_file():
        saved = json.loads(target.read_text(encoding='utf-8'))
        if saved.get('fingerprint') != digest or saved.get('image_sha256') != identity:
            raise ValueError('场景观察缓存版本不符')
        validate_observation(saved.get('observation'))
        return saved
    if cached_only:
        return None
    with Image.open(io.BytesIO(data)) as image:
        image.thumbnail((1600, 1600))
        encoded = io.BytesIO()
        image.convert('RGB').save(encoded, format='JPEG', quality=92)
    media = [dict(mime_type='image/jpeg', data=base64.b64encode(encoded.getvalue()).decode('ascii'))]
    if ask:
        value = ask(SYSTEM, {'task': 'describe_visible_scene'}, media)
    else:
        from .gemini_client import generate_gemini_text, parse_json_response
        value = parse_json_response(generate_gemini_text(system_prompt=SYSTEM,
            user_prompt='只依据图片记录可见物体、原样文字与实际表现，不作合格判断。',
            image_data=media, temperature=.1, max_output_tokens=2500,
            response_mime_type='application/json', json_root='object'))
    try:
        validate_observation(value)
    except ValueError as exc:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.with_suffix('.rejected.json').write_text(json.dumps(
            dict(fingerprint=digest, image_sha256=identity, error=str(exc), response=value),
            ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    saved = dict(fingerprint=digest, image_sha256=identity, observation=value)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)
    return saved
