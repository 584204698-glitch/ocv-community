"""Shared timing direction for first planning and explicit project revision."""
import re


def speech_budget(turns):
    """Count text in code so the model does not guess character counts."""
    rows=[]
    for index,turn in enumerate(turns):
        count=len(re.findall(r'[\u3400-\u9fff]',turn['text']))
        rows.append(dict(turn_index=index,chinese_characters=count,
                         natural_seconds_min=round(count/4,2),natural_seconds_max=round(count/3,2)))
    return rows


def story_speech_budget(text):
    """Supply quoted source words as a pacing reference, never invented dialogue."""
    quotations = re.findall(r'“([^”]+)”|「([^」]+)」', str(text))
    words = [a or b for a, b in quotations]
    count = sum(len(re.findall(r'[\u3400-\u9fff]', line)) for line in words)
    return dict(quoted_chinese_characters=count,
                quoted_natural_seconds_min=round(count / 4, 2),
                quoted_natural_seconds_max=round(count / 3, 2),
                note='引号可能包含标题或强调；仅供内容量参考，不作为台词提取、时长硬限制或删改依据。')


def storyboard_pacing(shots):
    """Measure whole-film handoffs; silent time is evidence, not a rejection."""
    clock, utterances, rows = 0.0, [], []
    for shot in shots:
        plan = shot.get('native_dialogue') or {}
        duration = float(plan.get('duration') or 0)
        turns = plan.get('turns', [])
        spoken = sum(float(t['end']) - float(t['start']) for t in turns)
        rows.append(dict(shot_id=shot['id'], duration=duration,
                         speech_budget=speech_budget(turns),
                         planned_speech_seconds=round(spoken, 3),
                         non_speech_seconds=round(duration-spoken, 3)))
        for turn in turns:
            utterances.append(dict(shot_id=shot['id'], character_id=turn['character_id'],
                                   start=clock+turn['start'], end=clock+turn['end']))
        clock += duration
    gaps = [dict(after_shot=a['shot_id'], before_shot=b['shot_id'],
                 seconds=round(b['start']-a['end'], 3))
            for a, b in zip(utterances, utterances[1:])]
    return dict(planned_seconds=round(clock, 3), shots=rows, speech_handoffs=gaps,
                note='无台词不等于冷场；结合原稿和动作检查重复等待，不以比例否决作品或新增台词。')


TIMING_POLICY = '''
native_dialogue.duration必须为4至15的整数秒；turns与performance_beats的起止时间允许小数秒。先保留自然台词与动作时序，再向上取整镜长；不足一秒的尾段只保持既定结束状态，不增加事件、拉长发言或重复动作。
时长与节奏规则：先数本镜实际中文台词，按每秒约3至4字估算自然发言，再为必要的独立动作和短呼吸分配时间，最后求镜长，不能先定长镜再填满。15秒是上限，不是推荐时长。旁白期间已有角色可以完成不需要开口的简单动作，避免将可以同时发生的事情串成等待。
不能用长时间端详、保持微笑、凝视、等回应等没有新叙事作用的状态凑尾巴。确有悬念、观察物理变化、人物迟疑等剧情依据才保留较长停顿，并说明依据。语速需适合年龄和情绪，不能为缩短而抢读、漏字。听者反应不默认单独占镜。
按全片检查问答衔接，而非只看单镜是否合格：普通接话预留约0.2至0.6秒；跨镜回应之前若有递物、行走等原稿必要动作，只保留动作实际需要的时间，不把每个准备、操作、直身、凝视再各拆成四秒镜头。同一机位下能连续完成的短动作合并；重要听者反打保留，但不要重复已经表达过的状态。有叙事依据的情绪停顿可以更长，不把此参考变成机械限时。
source_speech_budget是采用稿引语的程序统计参考。内容很短而期望片长很长时，以原稿内容决定紧凑片长，不能填满目标秒数。完整保留原稿的冲突、动机和所有直接引语，不能只抽两个标志句丢掉铺垫；采用稿本来只有短片段时不要编造对白充数。
静默段的观看内容按已有因果判断：物件被发现、视线转向、对方作出选择、接触发生、接收者反应，都可能推进故事。仅有风吹头发、呼吸、镜头移动不自动成为新的剧情变化。原作长停顿可能依赖此前铺垫、声音和剪辑；不能直接照搬秒数到缺少这些条件的短片。允许有依据的静止，不为证明“有动”添加无关手势、事件或台词。
发言、动作、镜长必须共同规划，performance_beats连续覆盖0至duration，每句turn完整对应一个节拍。4秒是H3下限，低于下限的内容不要编造新动作来填空。超过15秒时首轮规划按语义拆镜；固定镜数的二次编辑则明确报告限制，不擅自拆镜或删除台词。
只设计本镜物理事件。对白中的愿望、比喻、内心或未来事件不自动成为可见人物、头像、物件或蒙太奇；道具只处于本镜的阶段，身份参考不是新增演员。保持说话者与听者关系简单明确。
H3可执行性：一个节拍只围绕一个主要可见变化，不同时要求多人说话、多人手部操作和道具大幅变形。把“光芒”“脸”“拥抱”等比喻在动作描述中还原为本镜实际材质、形状、接触与位移，原文台词逐字保留。非发言道具上的眼睛嘴巴等装饰不自动获得说话或自主动作。发面、生长、烘烤等长时过程在首轮规划优先用明确的前后状态和时间过渡安排；已有核心图和时序的二次编辑不能偷偷改开场或删事件，只能简化实现动作，必要时给出局部重绘/拆镜建议供用户选择。发言结束后写明闭嘴回到静止下颌，接着执行必要的无声动作，不增加口头反应。
'''
