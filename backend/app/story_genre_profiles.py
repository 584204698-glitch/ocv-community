"""Fixed genre supplements selected from the adopted brief, without model calls.

Art style is deliberately not a genre selector. Unknown/imported briefs use
the general agent; a profile cannot change an adopted manuscript or contracts.
"""
from __future__ import annotations

REVISION = 'genre-profiles-v2-observed-response'

# Each supplement is small enough to coexist with the shared output contract.
PROFILES = {
    'general': ('', ''),
    'picturebook': (
        '以孩子能理解的愿望、尝试和具体感受推进；重复形成节奏，每次都有新反应，结尾不强加道理。',
        '给关键物件、动作结果和孩子的反应清楚的观看时间；拟人遵循采用稿，不把内心想象画成额外人物。'),
    'warm_relationship': (
        '用一次具体选择及对方的反应表现关系变化；安静收尾可以成立，不强求悲剧或反转。'
        '先让观众理解物件或行为为何重要，再以人物选择兑现；不能只留下求助与答应两句，省掉采用稿已有的前因。',
        '先呈现收到信息，再呈现回应；用视线、递物、距离变化表现关系，不靠新增独白或夸张哭笑解释。'
        '无声段分清注意到什么、对方做出什么、接收者如何改变；已有因果允许小幅表演，不要求不停移动。'
        '关键接触用手或物件近景看清结果，随后给接收者反应；不能每一步都展开成独立四秒等待，也不把多个机位塞进一个H3连续镜头。'),
    'micro_mystery': (
        '按疑问、已有线索、解释兑现组织；观众能回看理解线索，不靠结尾新人物或新证据解围。',
        '线索出现与人物发现分清时序；核心图保留必要可见证据，不能提前揭晓或在换镜时更改道具证据。'),
    'fantasy_rule': (
        '先通过具体事件建立规则和限制，再让选择造成后果；不能结尾临时增加能力。',
        '明确现实、梦境或幻想的进入与退出；只呈现已采用的超自然变化，内心话不生成分身或气泡人像。'),
    'miniature_world': (
        '让尺度、用途和物件行动参与因果，不只展示漂亮世界；空间与尺寸关系稳定。',
        '明确角色与物件的相对尺度及可通行路径；交互先接触再产生结果，禁止无依据放大缩小或复制。'),
    'everyday_comedy': (
        '人物认真解决具体麻烦，尝试带来新的偏差；以反应兑现笑点，不解释笑点，不重复同一失败拖时长。',
        '按铺垫、行动、结果、反应安排节拍；关键笑点结果需看得清，停顿有目的，不用长时间站立凑时长。'),
    'speculative_life': (
        '通过一次使用交代一项技术的用途及限制，实际选择带来生活后果；不写设定讲解或临时新增功能。',
        '技术效果用既定物件与可见状态变化表现，保持操作前后对应；不自行生成复杂界面文字或新增机器。'),
    'knowledge_story': (
        '围绕一个可靠知识点，具体疑问、尝试、观察结果依次出现；虚构或拟人不能作为科学证据。',
        '保留操作顺序与观察条件，结果在操作后出现；不跳过关键步骤，不把解说比喻直接变成物理变化。'),
}


def profile_id(brief):
    value = brief.get('preset') if isinstance(brief, dict) else None
    return value if isinstance(value, str) and value in PROFILES else 'general'


def production_profile(parameters):
    snapshot = parameters.get('story_snapshot') if isinstance(parameters, dict) else None
    return profile_id(snapshot.get('brief') if isinstance(snapshot, dict) else None)


def supplement(identity, stage):
    identity = profile_id({'preset': identity})
    if stage not in {'writer', 'director', 'review'}:
        raise ValueError('未知题材提示词阶段')
    writing, staging = PROFILES[identity]
    content = writing if stage == 'writer' else staging
    if not content:
        return ''
    boundary = ('仅检查采用稿在分镜中的表达，不要求作者改题材或补造事件。' if stage == 'review'
                else '保留用户明确要求；制作阶段不得改写已采用故事、台词、身份与音轨路线。')
    return f'\n固定题材规则（{identity}，{REVISION}）：{content}{boundary}\n'
