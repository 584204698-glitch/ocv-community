"""Explicit final H3 prompt adoption bound to frozen dialogue inputs."""
import re
import copy


def retire_after_action_replan(shot):
    """An explicitly replanned action must use its newly generated prompt."""
    manual = shot.pop('manual_h3_prompt', None)
    if not manual:
        return False
    shot.setdefault('manual_h3_history', []).append({
        **copy.deepcopy(manual), 'reason': '用户已重新规划本镜动态方案'})
    shot['manual_h3_history'] = shot['manual_h3_history'][-5:]
    return True


def validate(prompt, compiled):
    prompt=prompt.strip()
    baseline=compiled['prompt']
    for label in ('subject_definitions:', 'detailed_description:', 'overall_soundscape:'):
        if label not in prompt:
            raise ValueError('请保留 H3 结构字段：'+label)
    def definitions(text):
        return text.split('subject_definitions:',1)[1].split('summary:',1)[0].strip()
    if 'summary:' not in prompt or definitions(prompt)!=definitions(baseline):
        raise ValueError('角色、参考图及声音定义需保持原样；请只编辑动作和镜头描述')
    def lines(text):
        return [re.sub(r'\s+','',v) for v in re.findall(r'<d>(.*?)</d>',text,re.S)]
    if lines(prompt)!=lines(baseline):
        raise ValueError('手动稿的台词及顺序必须与已有音轨一致；请保留原 <d> 台词块')
    pictures=[int(n) for n in re.findall(r'<Picture\s+(\d+)>',prompt)]
    allowed=set(int(n) for n in re.findall(r'<Picture\s+(\d+)>',baseline))
    if set(pictures)-allowed:
        raise ValueError('手动稿引用了本镜没有上传的参考图')
    for token in ('Subject', 'Audio'):
        if set(re.findall(r'<'+token+r'\s+(\d+)>',prompt))-set(re.findall(r'<'+token+r'\s+(\d+)>',baseline)):
            raise ValueError('手动稿新增了未绑定的角色或声音标签')
    return prompt


def adopted(shot, compiled):
    manual=shot.get('manual_h3_prompt')
    if not manual:return None
    if manual.get('fingerprint')!=compiled['fingerprint']:
        raise ValueError('手动 H3 稿对应的图片、音轨或动作已变化，请重新核对并采用手动稿，或恢复自动提示词')
    return validate(manual['prompt'],compiled)
