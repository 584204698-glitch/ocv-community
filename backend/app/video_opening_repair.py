"""Turn a retained opening-image conflict into a local edit, without inference."""
import hashlib
import json
from pathlib import Path

MARKER = '开场图与已批准动作不一致，尚未提交H3：'


def design_digest(shot):
    data = {key: shot.get(key) for key in
            ('image_prompt', 'visual_description', 'action', 'continuity_states', 'cast_plan')}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def capture(path, shot):
    error = str(shot.get('video_error') or '')
    if MARKER not in error:
        return None
    evidence = error.split(MARKER, 1)[1].strip()
    opening = str(shot.get('visual_description') or '').strip()
    if not evidence or not opening or not shot.get('image'):
        return None
    root = Path(path).resolve()
    image = (root / shot['image']).resolve()
    if not image.is_relative_to(root) or not image.is_file():
        return None
    suggestion = dict(evidence=evidence, opening=opening,
                      image=shot['image'], image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                      design_sha256=design_digest(shot),
                      image_prompt=shot.get('image_prompt'), action=shot.get('action'))
    shot['opening_image_repair'] = suggestion
    return suggestion


def prepare(path, shot):
    suggestion = shot.get('opening_image_repair') or {}
    root = Path(path).resolve()
    image = (root / str(shot.get('image') or '')).resolve()
    if (not suggestion or suggestion.get('design_sha256') != design_digest(shot)
            or suggestion.get('image') != shot.get('image')
            or not image.is_relative_to(root) or not image.is_file()
            or hashlib.sha256(image.read_bytes()).hexdigest() != suggestion.get('image_sha256')):
        raise ValueError('图片或本镜设计已变化，旧开场诊断不再适用；请检查当前画面。')
    prompt = ('图1是本镜当前核心图。只做局部修图，不生成动作过程或拼贴。\n'
              '【修复目标】以下已批准开场要求是正确目标：\n' + suggestion['opening']
              + '\n【需要纠正的偏差】以下描述是错误现状，不得照着重画：\n' + suggestion['evidence']
              + '\n将相关角色或物件调整到已批准的开场空间关系及状态。保留其余正确的构图、'
                '角色身份、服装、画风、光线和道具；不新增人物，不提前画出后续动作结果，'
                '不改故事、台词或声音，不生成文字。')
    return image, prompt
