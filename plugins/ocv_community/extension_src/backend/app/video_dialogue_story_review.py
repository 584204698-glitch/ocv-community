"""Review the performed story, independently of renderability and field validity."""
from __future__ import annotations

import copy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REVISION = 'native-dialogue-story-v5-adopted-response-contract'


class Problem(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    code: Literal['missing_event', 'wrong_speaker', 'changed_meaning', 'unnatural_dialogue',
                  'missing_motivation', 'unearned_ending', 'empty_repetition']
    shot_ids: list[str] = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=8, max_length=1600)
    repair: str = Field(min_length=8, max_length=1600)
    origin: Literal['adaptation', 'adopted_story'] = 'adaptation'


class StoryReview(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    reviewed_shot_ids: list[str] = Field(min_length=1, max_length=100)
    viewer_question: str = Field(min_length=8, max_length=1200)
    motivation_and_payoff: str = Field(min_length=8, max_length=2000)
    problems: list[Problem] = Field(max_length=30)


SYSTEM = '''你是独立的中文故事对白编辑，审查完整故事会怎样被观众看到、听到。
输入含采用原稿和真正将交给H3的镜头/台词。source_text保留了原文，不代表它已被表演出来。
只用各镜实际可见动作、角色逐句台词、混合模式实际画外旁白判断观众获得的信息。
检查关键事件、人物为什么行动、前因后果与自然结局是否被漏掉；话是否说错人，直接引语是否被篡改。
纯对话不能依赖观众看不见的原文旁白解释；混合模式不让角色突然念旁白。
按采用稿判断谁需要说话：一名角色发言、另一名角色用明确行动回应也可成立，不强制每个听者开口，不为达到多人对白而添加台词。只有采用稿实际要求多轮问答而分镜遗漏或转为独白时才报告unnatural_dialogue；喷嚏、笑声、叹气不能冒充采用稿已有的另一人的对白。采用稿本身的对白数量或交谈偏好建议标记origin=adopted_story，不能让分镜导演加词修复原稿。
混合模式和儿童绘本允许简短的动作伴随叙述、象声词与节奏性重复，画面和话语有所重合不自动构成缺陷。
先检查旁白是否补充画面无法确认的时间、关系、动机、情绪或背景；不能把由旁白提供的背景信息当作画面已独立证实的事实。
empty_repetition只用于持续复述造成情节停滞、挤占必要对白/反应或重复同一信息却无节奏作用的具体问题；指出受影响的节拍和实际代价，不能仅以“旁白描述了可见动作”判失败。
storyboard_pacing给出逐镜台词预算、无台词时间和跨镜接话间隔。沿全片检查求助、问答、反问与回应之间是否被重复凝视、准备和结束姿态拖散；必要的递物、行动、悬念与情绪停顿不是缺陷。不能仅凭对白占比判失败，不能为填时间增加台词；确有冗余时指出具体节拍，建议合并短动作或缩短无事发生的等待。节奏建议仍按empty_repetition与既有建议政策处理，不新增生产阻断。
判断静默段是否承接了已建立的物件意义、人物选择和对方反应；人物不动也可有叙事作用。不要把风吹、呼吸或运镜自动当作情节推进，也不要仅因缺台词要求人物说话。参考原作的停顿时必须有当前故事依据，不能将另一个作品的具体秒数当作本片合格标准。无声口型与镜头实际表现需成片证据，本轮不能据脚本声称已经验证。
不要把混合模式修成零旁白，也不要为了让旁白与画面绝不重复而要求作者编造原稿没有的新事实。
说明开头让观众想知道什么、后续如何发展并兑现。逻辑成立或有六镜并不自动等于有观看动力。
问题必须指出当前镜头/台词的具体内容，不凭题材偏好打分，不强求反转、大冲突或说教。
自然日常小事、绘本童趣、安静温暖的收尾可以合格。原稿有缺陷须说明，不偷偷改写原稿设定来通过。
每项问题标明origin：adaptation表示分镜相对采用稿新增的错误、遗漏或不一致；adopted_story表示采用稿自身已有的疑点。原稿内的梦境暧昧、开放结尾、象征表达与主题强弱属于作者选择，不要求生产流程补写原稿没有的事件来证明主题。原稿建议只供作者参考，不要求分镜导演代作者改稿。
区分合理的叙述转对白与改变事实；允许自然短句，不允许凭空增加关键事件或人物动机。
无音视频输入，不能声称听到稳定音色、看到口型正确或成片合格。
返回严格JSON：{reviewed_shot_ids:[全部镜头id，按顺序],
viewer_question:"指出开头的具体观看期待及后续发展，或缺失在哪里",
motivation_and_payoff:"结合实际台词/动作说明动机和结局是否得到表达",
problems:[{code,origin:"adaptation或adopted_story",shot_ids:[涉及镜头id],evidence:"引用具体动作/台词，说明为何影响理解或观看",
repair:"保持采用故事事实的可执行修订，原稿本身有问题则指出需用户改稿"}]}。
code仅允许missing_event/wrong_speaker/changed_meaning/unnatural_dialogue/missing_motivation/unearned_ending/empty_repetition。
没有确切问题时problems为[]；不要把审查语言写进故事台词。
'''


def review_story(context, shots, *, ask):
    from .story_genre_profiles import supplement
    from .video_timing_policy import storyboard_pacing
    result = StoryReview.model_validate(ask(SYSTEM + supplement(context.get('story_genre_profile'), 'review'), {
        'adopted_story': context['production_text'],
        'speech_mode': context['native_speech_mode'],
        'cast': context['project_cast'],
        'performed_shots': copy.deepcopy(shots),
        'storyboard_pacing': storyboard_pacing(shots),
    })).model_dump(exclude_defaults=True)
    expected = [s['id'] for s in shots]
    if result['reviewed_shot_ids'] != expected:
        raise ValueError('对白故事审核未覆盖完整镜头，不能放行生成')
    for problem in result['problems']:
        if len(set(problem['shot_ids'])) != len(problem['shot_ids']) or not set(problem['shot_ids']) <= set(expected):
            raise ValueError('对白故事审核引用了错误镜头，不能据此改写故事')
    return {'revision': REVISION, **result}


def partition_problems(review, *, strict=False):
    """Keep authorial suggestions visible without asking production to rewrite them.

    Legacy reviews have no origin, so their subjective codes follow the same
    default advisory policy. No phrase matching or story-specific exceptions.
    """
    blocking, advisory = [], []
    editorial = {'missing_motivation', 'unearned_ending', 'empty_repetition'}
    for problem in (review or {}).get('problems', []):
        suggestion = (problem.get('origin') == 'adopted_story'
                      or (not strict and problem.get('code') in editorial))
        (advisory if suggestion else blocking).append(copy.deepcopy(problem))
    return blocking, advisory
