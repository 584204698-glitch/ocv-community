"""Synchronize editable staging with H3's actual native-dialogue input."""
from __future__ import annotations

import copy
from pydantic import BaseModel, ConfigDict, Field

from .h3_dialogue import validate_dialogue_draft, PerformanceBeat, DialoguePlan, performance_intervals


class Position(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    character_id: str = Field(pattern=r'^pc_[a-f0-9]{32}$')
    position: str = Field(min_length=1, max_length=240)


class Staging(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    scene: str = Field(min_length=8, max_length=4000)
    soundscape: str = Field(min_length=1, max_length=800)
    positions: list[Position] = Field(max_length=8)
    action: str = Field(min_length=1, max_length=4000)
    opening: str = Field(min_length=1, max_length=2000)
    image_prompt: str = Field(min_length=1, max_length=6000)
    prop_boundaries_compatible: bool | None = None
    performance_beats: list[PerformanceBeat] | None = Field(default=None, max_length=13)


SYSTEM = '''你是 H3 Ref2VA 原生对话镜头的动作同步员。只修订本镜，不走旁白静音路线。
frozen_dialogue 的所有说话者、逐字台词、发言时间、音色和镜头时长已确定，绝对不改写、不添加。
每位角色的presence也已确定：later角色只在既定入场或露面后可见，开场核心图不能提前画出。
continuity_props是已固定的关键道具外观与结构，保持其身份；位置、开合、损坏、占用等状态按本次动作与开场描述，不照搬旧状态。
approved_scene_design是已有固定布景的设计依据，不能重新设计它。局部改构图时，中文opening和image_prompt须保留画内布景上已批准短字的逐字内容和原语言，不能缩成“原有横幅/标牌”；墙面确实出画时不强求显示文字。core_basis中的不变细节同样保留，不能把未提及理解成删除；有设计冲突时不要擅自换字或补造文字。
scene、soundscape、positions.position、performance_beats.action仍严格英文，中文场景短字不要抄入这些字段；用英文描述对应载体并要求保持核心图中已有字样不变，不翻译字样内容，不把字样变成对白。准确原字只放中文图像设计字段；turns的中文台词仍完全冻结。
若输入有continuity_states，则它们是本镜已批准的跨镜开场/结束边界；本次动作应连接两者，不改变无关物件的状态。返回prop_boundaries_compatible说明本次修改是否能满足这些边界。不能满足时返回false，不假装旧状态仍适用。
H3 直接生成这些话；人物只在自己的发言时段说话，其他时刻倾听。不得追加静音、无对白或后期配音要求。
人工 manual_action 是本次动作修改要求。保持角色身份、年龄、服装、场景和连续单镜，不新增人物或分身。
若只是 video_inputs 同步，保留原动作和事件顺序，只校正实际参考图角色和空间对应，不另编故事。
core_basis.kind=image_analysis 时，只把已识别的像素事实作为开场依据；image_prompt 是设计文案，不是像素证明。
scene、soundscape、positions.position 严格英文；不在scene中复述对白原文，编译器会插入精确台词标签。
scene只写固定场景、机位与开场空间关系。动作路径、手与物体关系及结果放入performance_beats，一个主要动作和自然反应，不增加额外剧情、背景音乐。
performance_beats只写本镜可见的动作及会产生明确声音的画外事件。明确不会入画、不会发声的画外角色准备动作不交给H3演，保留在跨镜连续性边界中；不能把“某人在门后藏东西且不入画”写成当前可见动作。后续角色只能在本镜已批准的实际入场之后描述，画外台词和声响仍按既定时间保留。
按H3可执行的物理动作设计，不让模型推理比喻、愿望或人物内心。台词/旁白中提到的人物、回忆、将来物件和“脸”等比喻不是新增可见实体；只有本镜明确批准的可见事件才进入动作。内心活动用已有角色一个自然表情承接，不新增头像、气泡、分身或蒙太奇。
continuity_props是全故事道具资料，可能含后续阶段；仅提取符合本镜continuity_states和真实核心图的特征，不把烤好、装饰完成等未来状态写进当前动作。实际状态变化必须有本镜明确动作和结束边界支持。
每个节拍只描述承担故事含义的直接动作，说明动作主体、接触对象和可见结果。不要把一次指向扩成转身、走位、递接等多项任务；人物原地说话时听者保持简单自然反应，不要求双方同时复杂动作。不为了填满既定时长新增剧情。
performance_beats按开场、发言期间、发言结束后的顺序描述动作，与已固定的turns时间一致；不重复角色外貌、音色、台词原文和禁止说话规则。
position写站位或入场位置，不复制整段动作。中文action与performance_beats必须表达同一事件顺序；固定道具外观和状态由程序附加，scene不要再抄其目录。
action、opening、image_prompt写中文，image_prompt只画真实开场，不画多个阶段或角色设定图，不生成台词文字。
refresh_basis=action时，构图围绕本镜已有叙事主体明确：谁是视觉重点、人物可见到哪个部位、哪些道具或接触部位必须入画；opening、image_prompt和scene三者一致。不以“桌子的中景”代替人物取景，不为展示完整参考图而扩大景别。保留剧情要求的可见证据；不依赖的身体或背景区域可以在画外，不等于删除物件。取景须支持已固定动作和跨镜边界，不擅自把所有镜头改为半身。
refresh_basis=image或video_inputs时，不借构图优化重新设计已有像素事实或既定动作。
positions须为frozen_dialogue中每个非旁白角色恰好返回一次对应ID；旁白不占画面，不返回其position。
同时返回performance_beats，将0至duration按每句turn的完整[start,end]和所有非空时间间隙分段，每段{start,end,action,action_zh}。
action英文、action_zh中文，二者表达同一具体动作与先后关系。台词由程序插入，不在action重复台词/外貌/声音规则；scene只描述固定场景和开场关系。
说完才走等动作只能放在对应的发言后节拍，不能让H3自己猜时间顺序；所有节拍合计宜简洁，不增加剧情。
返回严格JSON：{scene,soundscape,positions:[{character_id,position}],action,opening,image_prompt,prop_boundaries_compatible,performance_beats:[{start,end,action,action_zh}]}。
'''


def refresh(context, style, shot, references, *, basis, action, image_prompt,
            image_path=None, force_vision=False, image_analysis=None, action_only=False,
            video_inputs=None, ask=None):
    from .video_agents import ask_json
    from .video_prompt_refresh import analyze_image
    ask = ask or ask_json
    if basis not in {'action', 'image', 'video_inputs'}:
        raise ValueError('未知的原生对话动作更新方式')
    original = validate_dialogue_draft(shot.get('native_dialogue'))
    analysis = image_analysis if force_vision and image_analysis else (
        analyze_image(image_path) if force_vision and image_path else None)
    core = analysis['description'] if analysis else (image_prompt or shot.get('image_prompt', ''))
    external = (context.get('video_direction') or {}).get('dialogue_route') == 'external_tts'
    system = SYSTEM
    if external:
        system = system.replace(
            'H3 直接生成这些话；人物只在自己的发言时段说话，其他时刻倾听。不得追加静音、无对白或后期配音要求。',
            '角色对白由已采用音色的 TTS 先合成，H3 接收对白音轨作为口型约束；'
            '不要求 H3 自行创作或克隆台词。角色只在其冻结的对白时段张嘴，其他角色倾听。'
            '旁白独立后期合成，不传给 H3，不引发画中人物说话或听见旁白的反应。'
            '保留全部冻结台词及时间；动作描述不得再添加未指定原文的念咒、嘟囔或额外回答。')
    result = Staging.model_validate(ask(system, dict(
        story_context=context, style=style, frozen_dialogue=original, refresh_basis=basis,
        continuity_props=copy.deepcopy(shot.get('continuity_props', [])),
        continuity_states=copy.deepcopy(shot.get('continuity_states', [])),
        approved_scene_design=copy.deepcopy(shot.get('approved_scene_design')),
        manual_action=action, previous_video_prompt=shot.get('video_prompt', ''),
        core_basis={'kind':'image_analysis' if analysis else 'image_prompt','description':core},
        references=references, video_inputs=video_inputs)))
    if shot.get('continuity_states') and result.prop_boundaries_compatible is not True:
        raise ValueError('本次动作尚未确认能衔接道具开场与结束状态，原镜头已保留；请核对相邻分镜后调整动作。')
    positions = {row.character_id:row.position for row in result.positions}
    identities = {row['character_id'] for row in original['speakers'] if row['kind'] == 'character'}
    if len(positions) != len(result.positions) or positions.keys() != identities:
        raise ValueError('动作同步改变了角色名单，已保留原镜头；请重新核对')
    plan = copy.deepcopy(original)
    plan.update(scene=result.scene, soundscape=result.soundscape)
    if result.performance_beats is not None:
        plan['performance_beats'] = [b.model_dump() for b in result.performance_beats]
    elif original.get('performance_beats'):
        raise ValueError('动作同步未返回更新后的表演节拍，原镜头已保留，请重试同步')
    for speaker in plan['speakers']:
        if speaker['kind'] == 'character':
            speaker['position'] = positions[speaker['character_id']]
    plan = validate_dialogue_draft(plan)
    updated = copy.deepcopy(shot)
    updated['native_dialogue'] = plan
    # Preserve explicit user wording and input-only sync wording verbatim.
    updated['action'] = action.strip() or result.action
    updated['image_prompt'] = result.image_prompt if basis == 'action' else image_prompt or shot.get('image_prompt', '')
    if basis == 'action':
        # The opening description is a design input to later image/grounding
        # reviews. Keep it aligned with this explicit design revision, without
        # treating the revised description as evidence of existing pixels.
        updated['visual_description'] = result.opening
    updated['video_prompt'] = ('图1是本镜构图和画风参考；角色按已合成对白音轨的时序做对应口型，旁白不传入H3。'
                               if external else '图1是本镜构图和画风参考；角色按已采用的音色说出本镜台词。') + updated['action']
    updated['video_prompt_warnings'] = []
    updated['native_staging_stale'] = False
    names = [s['name'] for s in plan['speakers'] if s['kind'] == 'character' and s['on_screen']]
    opening_names = [s['name'] for s in plan['speakers'] if s['kind'] == 'character'
                     and s['on_screen'] and s.get('presence', 'opening') == 'opening']
    updated['motion_plan'] = dict(version=2, scene_anchor=result.opening, participants=names,
        reference_participants=opening_names, reference_visual=result.opening, reference_beat=1,
        reference_texts=[], beats=[dict(action=updated['action'],texts=[])])
    if shot.get('continuity_props'):
        from .video_prop_continuity import apply
        updated = apply(updated, shot['continuity_props'])
    if shot.get('continuity_states'):
        from .video_prop_states import apply
        updated = apply(updated, shot['continuity_states'])
    return updated, analysis


def assert_staging_current(shot):
    if shot.get('native_staging_stale'):
        raise ValueError('本镜动作已修改，请点击“按动态表达更新提示词”，同步 H3 实际动作后再生成')


class PerformanceTimeline(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scene: str = Field(min_length=8, max_length=4000)
    performance_beats: list[PerformanceBeat] = Field(min_length=1, max_length=13)


def complete_performance(shot, *, ask=None):
    """One bounded preparation call on explicit generation; never on browsing.

    Legacy/newly edited dialogue gets timed actions from its saved design.
    The model cannot modify words, speakers, reference bindings or duration.
    Failure keeps the saved shot and stops before submitting a GPU task.
    """
    original = validate_dialogue_draft(shot['native_dialogue'])
    if original.get('performance_beats'):
        return original
    from .video_agents import ask_json
    ask = ask or ask_json
    plan = DialoguePlan.model_validate(original, context={'unbound_draft': True})
    intervals = performance_intervals(plan.duration, plan.turns)
    prompt = '''将已保存的H3镜头动作和台词排成同一条表演时间线，不创作新故事。
返回scene和performance_beats，不修改人物名单、开场事实、事件、台词、音色或时长。
scene只用英文写已批准的固定场景、机位与开场空间关系；把旧scene中的动态过程移入performance_beats，不在两处重复动作。
不在scene中抄写Object identity constraints或Prop state continuity目录，程序会原样保留这些已批准约束。
每个required_intervals恰好一段，顺序和时间完全一致；每段含start,end,action（英文具体动作）,action_zh（同义中文）。
按最终视频提示词、已冻结的英文场景和人物位置安排：谁在说话、面向谁、听者如何反应，发言结束后才开始的动作不得提前。最终视频提示词是动作创作依据，不从旧分镜动作或图像设计补回已舍弃的事件。
安静间隙不添加对白或配音。不要重复台词、外貌、声音禁令或道具目录；使用人物称谓明确指代。
移动写清从哪里沿什么固定参照走到哪里、在哪里停；用最短合理路径，不绕圈，不让机位移动冒充人物移动。
只表达原动作支持的信息，不能凭空编造精确方位、障碍或肢体交互；保留已批准的开场和结束道具状态。
各段action合计约80至180个英文词即可，action_zh必须准确对应，不能中英文各写不同动作。
输出严格JSON：{scene,performance_beats:[{start,end,action,action_zh}]}。'''
    result = PerformanceTimeline.model_validate(ask(prompt, {
        'frozen_dialogue': original, 'required_intervals': [dict(start=a,end=b) for a,b in intervals],
        'approved_video_prompt': shot.get('video_prompt',''),
        'continuity_states': copy.deepcopy(shot.get('continuity_states',[])),
    }))
    candidate = copy.deepcopy(original)
    # Preserve program-owned prop definitions byte-for-byte; the preparation
    # model may reorganize staging prose, not redefine physical boundaries.
    markers = (' Object identity constraints: ', ' Prop state continuity: ')
    if any(marker.strip() in result.scene for marker in markers):
        raise ValueError('表演时间线准备重复了道具目录，原镜头已保留，请重试')
    offsets = [original['scene'].index(marker) for marker in markers if marker in original['scene']]
    suffix = original['scene'][min(offsets):] if offsets else ''
    candidate['scene'] = result.scene + suffix
    candidate['performance_beats'] = [b.model_dump() for b in result.performance_beats]
    return validate_dialogue_draft(candidate)
