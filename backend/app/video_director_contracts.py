"""Video adaptations of the proven image-story director rules.

Derived from story_agents.ENHANCED_DIRECTOR_AGENT1_CONTRACT and
module4_video_render.ENHANCED_DIRECTOR_AGENT2_CONTRACT. Keep the image
pipeline independent: video timing and motion contracts differ.
"""

VIDEO_DIRECTOR_REVISION = 'single_core_v8_20261012_story_medical_integration'

# Verified opening from the user's September 17 single-reference video test.
# Assemble before saving the editable prompt, never at provider submission time.
SINGLE_REFERENCE_GUIDE = (
    '图1是单张复合内容参考，不是必须照搬的首帧，也不表示所有内容同时存在。'
    '按提示词把图中的元素拆成先后阶段，输出始终是普通全屏视频。'
)

OPENING_FRAME_GUIDE = (
    '图1是本镜开场的真实首帧，开场只保留图中已经可见的主体与状态。'
    '按原文顺序展开后续动作，后入场人物不得提前出现在开场；'
    '另行提供的人物身份参考或尾帧只约束相应人物及阶段，不表示全员开场在场。'
)

COMPOSITION_REFERENCE_GUIDE = (
    '图1是本镜构图、画风、材质、光线与配色的参考锚点，不是严格首帧或像素锁定。'
    '其余图片仅按已采用清单约束对应主体的身份和服装，不复制无关背景；'
    '保持既定事件顺序，后入场人物按相应阶段再出现，不要求所有参考人物开场同时在场。'
)

FIRST_LAST_GUIDE = (
    '图1是本镜真实首帧，图2是真实尾帧；沿既定动作顺序从首帧逐步过渡到尾帧。'
    '图2不是独立人物身份参考，不将尾帧人物或结束状态提前搬到开场；'
    '尾帧图片是否包含所需人物仍须核对实际像素。'
)

REFERENCE_AVAILABILITY_CONTRACT = """【参考输入状态与剧情分离】
项目身份绑定、核心图制图采用、实际视频输入采用是三类不同事实。project_cast 中绑定了图片，
不代表本镜视频已经采用；未列入本镜编号表也不代表项目没有该资产。
旧 action、motion_plan、video_prompt 中的“本镜未提供身份图”等输入状态可能已经过期，
这类制作状态只放在 warnings 等元信息中，不写进 beats.action、动作正文或最终视频提示词。
具有剧情含义的“没有照片”等原文不能因相似词语被删除。状态修订不改变原事件与角色约束。
参考图被采用不证明人物在图片中出现，也不能生成 user_confirmed、in_last_frame 或图片覆盖确认。
"""

STORY_NARRATION_AUDIO_RULE = (
    '旁白后期合成，不生成额外对白或背景音乐；保留原文动作对应的自然环境/动作声音。'
    '原文中的角色台词仍由后期配音朗读，视频只用无声表情、视线和手势表达其含义；'
    '不要把“说出、对大家说、开口回答”或台词引号直接写成视频发声指令，不做连续说话口型。'
    'semantic.speech_turns 仍如实保留原文说话者和内容，语义归属不等于要求视频生成声音。'
)

STORY_OPENING_FRAME_CONTRACT = """【故事与项目角色的真实开场首帧契约】
若提供 story_handoff，它标识已采用故事的制作来源；project_cast 提供已经绑定参考图的项目身份，来源以 source 字段为准。
当前正文、已提供的明确制作要求和本镜 source_subtitles 是事实依据；
人物档案、摘要和参考图文件名不构成出镜名单。kind=video 的核心图必须是真实开场首帧，
只画本镜开场已可见的人物、物体、文字和状态，不把后入场角色、后续事件或结果提前塞入首帧。
动作已经开始的真实开场可以作为首帧，不为了做首帧把它退回空泛准备姿势。静态镜头按本镜原文设计。
motion_plan.reference_beat 必须为 1；reference_participants 仅列开场可见主体，
reference_texts 仅列开场已可见短字，不能从后续阶段收集素材拼进首帧。
participants 表示整镜计划涉及的主体，与 reference_participants 的差集仅是文字计划差异，
不自动证明它们是人物、实际后入场角色或图片中有谁；不得据此创建 user_confirmed 或像素确认。
后入场人物应在对应原文阶段才出现。保持其身份需要实际人物身份参考，或确实含该人物的真实尾帧；
中途出现又离场者不能靠无该人物的尾帧解决。参考图只能使用本次提供的真实 id，不能按同名猜绑定。
缺少或未确认图片依据时明确保留身份输入待核对的缺口，不声称已锁定，不让视频模型仅凭文字自由发明外观。
独立身份参考可以包含后入场人物，但它是身份条件，不要求这些人物同时出现于真实首帧。
""" + STORY_NARRATION_AUDIO_RULE + '\n'


def story_uses_opening_frame(context) -> bool:
    handoff = context.get('story_handoff') if isinstance(context, dict) else None
    cast = context.get('project_cast') if isinstance(context, dict) else None
    if cast is not None:
        # A cast-only legacy manuscript also needs a real opening frame. A
        # malformed/model-invented snapshot must not silently enable this path.
        from .video_cast_planning import validate_planning_cast
        validate_planning_cast(cast)
        return True
    return isinstance(handoff, dict) and handoff.get('status') in ('current', 'legacy_body_only')


def story_frame_contract(context) -> str:
    if not story_uses_opening_frame(context):
        return ''
    from .video_renderability import STORY_STAGING
    return STORY_OPENING_FRAME_CONTRACT + STORY_STAGING


PROJECT_CAST_CONTRACT = """【项目角色身份与本镜出场计划】
story_context.project_cast 是已保存的项目角色与真实参考素材绑定，独立于模型整理的 characters。
source=auto_designed_project_identity 表示系统按本故事设计并绑定，不能声称用户已逐图确认；
source=user_confirmed_project_identity 表示用户明确保存的身份选择。
project_character_id 是本项目稳定身份，reference_id 是明确绑定的素材；只使用提供的精确 ID，
不能按同名、文件名、外形猜测补绑，也不能把模型自行生成的 character_id 当作项目角色 ID。
逐字保留该角色 constraints 中明确的服装、配色、身体结构与身份限制；不因动作或场景方便自行改装。
绑定证明本项目具有对应身份参考，不证明角色出现在任何首帧、尾帧或生成结果，也不等于已经锁定输出外观。
当前真实绑定的图片可用性由 project_cast 提供；采用时要求中的“图片尚未提供”可能只是当时状态，
不得据此否认本次已经提供的真实素材。若两处明确造型要求相互冲突，保留冲突待核对，不擅自改写原稿或设定。
项目绑定不等于本镜实际视频采用；缺图或待核对状态只放 warnings，不写入 beats.action 或剧情正文。
角色名单不等于出镜名单。只依据本镜 source_subtitles 及全文顺序安排实际可见角色，
仅被提及、未在本镜出现者不列入本镜角色计划；资料不清时保留未知，不凭同名挑选一个绑定。
Agent 2 每镜输出 planned_characters 数组，每项仅含 project_character_id、presence、basis；
presence 为 opening 或 later，basis 摘出本镜真实原文依据并简述出场顺序。没有项目角色出镜则明确填 []。
opening 表示计划在开场可见，later 表示计划在后续阶段才入场；它们是导演文字计划，不是像素确认。
核心图只画 opening 角色；later 角色不能为补齐身份条件被提前画入核心图。
reference_ids 是本镜核心图采用的素材：开场人物只采用其绑定的精确 reference_id，
不要选择仅 later 或未声明出场角色的身份图来塞入首帧；后入场身份图由后续视频输入单独核对与采用。
动态与定稿阶段沿用已保存 cast_plan 的 ID、入场顺序与对应约束；不重新绑定人物或虚构已采用视频参考图。
planned_characters、cast_plan、人物名称清单以及两份清单的差集，都不能生成 user_confirmed、
in_last_frame、已确认图片覆盖等结论。具体图片中的出场及视频输入是否真正采用仍需原有图片核对流程。
"""


def project_cast_contract(context) -> str:
    cast = context.get('project_cast') if isinstance(context, dict) else None
    if cast is None:
        return ''
    from .video_cast_planning import validate_planning_cast
    validate_planning_cast(cast)
    return PROJECT_CAST_CONTRACT


def reference_guide(context, *, input_mode=None) -> str:
    if input_mode is not None:
        guides = {'single_first': OPENING_FRAME_GUIDE, 'first_last': FIRST_LAST_GUIDE,
                  'composition_reference': COMPOSITION_REFERENCE_GUIDE}
        if input_mode not in guides:
            raise ValueError('实际视频输入模式无效')
        return guides[input_mode]
    return OPENING_FRAME_GUIDE if story_uses_opening_frame(context) else SINGLE_REFERENCE_GUIDE


def video_input_contract(video_inputs) -> str:
    """Describe supplied inputs without asserting anything about their pixels."""
    return """【已采用的实际视频输入】
video_inputs 是服务端解析的实际有序图片清单；numbered_references 与此清单一一对应。
只使用该清单中的图号、ID、role；不能用 shot.reference_ids 的核心图制图列表替代它。
project_characters 是明确项目身份关联，不能按名称或文件名猜绑定；保留各角色服装、身体结构与身份限制。
图片清单和摘要只证明采用关系与文件版本，不是图片识别结果；不得据此虚构可见人物、尾帧覆盖或核对结论。
""" + reference_guide(None, input_mode=video_inputs['input_mode']) + '\n' + REFERENCE_AVAILABILITY_CONTRACT


def opening_frame_plan_issues(context, plan) -> list[str]:
    """Check declared phase fields only, never infer visible characters from text."""
    if not story_uses_opening_frame(context):
        return []
    if not isinstance(plan, dict) or plan.get('version') != 2:
        return ['当前动态镜头需要 version=2 的真实开场首帧方案']
    issues = []
    if type(plan.get('reference_beat')) is not int or plan['reference_beat'] != 1:
        issues.append('真实开场方案的 reference_beat 必须为 1，不能将后续阶段当作真实首帧')
    beats = plan.get('beats')
    opening_texts = beats[0].get('texts', []) if isinstance(beats, list) and beats and isinstance(beats[0], dict) else []
    if any(text not in opening_texts for text in plan.get('reference_texts', [])):
        issues.append('真实首帧包含第一阶段未声明的短字，不能提前显示后续阶段文字')
    return issues

NO_AUTO_SUBTITLES = (
    '禁止擅自生成任何字幕、自动字幕、底部字幕条、旁白文字转写、标题卡或水印。'
    '只有阶段方案明确批准的少量场景内文字可以按原文出现；不得把配音、旁白、动作说明或提示词复制成屏幕字幕。'
)


def enforce_no_auto_subtitles(prompt: str) -> str:
    prompt = str(prompt or '').strip()
    return prompt if NO_AUTO_SUBTITLES in prompt else (prompt + '\n' + NO_AUTO_SUBTITLES).strip()

SPEECH_ATTRIBUTION_CONTRACT = """【发言与想法的归属：不是谁配音就归谁】
先判断有没有需要表现的画面对话。story_context.speech_attribution 是全文归属依据；
semantic.speech_mode=none 或 on_screen=none 表示不需要画面人物发言，不因旁白、问号或第二人称
添加主持人、观众、张嘴动作、对口型、气泡或画中文字。已有角色可以做与讲解有关的动作，不必说话。
quoted、假设、模仿不是默认现场对话；unknown/uncertain 保留歧义，不自动派给主角。
语义与核心画面阶段每镜先写 semantic.speech_mode（none/dialogue/mixed/uncertain），
无画面对话填none和speech_turns=[]；其他情况只记录本镜有原文依据的关系。
区分旁白朗读者、原文中实际发言/持有想法的人、画面上承载该内容的人。旁白转述别人的提问、
评价或想法，不等于主角亲自提出、认同或说出了它；“讲者列举某观点”不是把该观点放到讲者气泡的依据。
先读原文的引语引导、称呼、我/你/他们、问答对象与上下文，判断谁向谁提问、谁回答、谁想象。
原文明说“大家问我”时是大家向我问，不反过来；若邀请受众说第一反应，后接的省略主语短答
可以是受众的典型回应，后续并列问答应承接这一关系，不能每换一例就变成讲者独白。
这不是关键词规则：作者自问自答、概念解释、无人互动的旁白不凭问号添加受众；跨镜引用须看
全文与相邻字幕恢复省略主语。归属不明写 unknown，不凭空造人、分配立场或把猜测当明确发言。
语义规划与核心画面阶段在 semantic.speech_turns 按原文先后记录有归属意义的发言/想法；后续动态
与定稿阶段只读取核对，不再输出这份清单。同一归属的连续话合为一项，只摘关键短语，不逐条抄字幕。每项只含：
source_text（本镜对应的短原文，不改写）、speaker（观点/提问的归属者，稳定短角色名，不是TTS音色）、
addressee（面向谁，无则空）、mode（spoken/thought/quoted/narration/unknown）、basis（一句原文或上下文依据）、
on_screen（dialogue/thought/none/uncertain）、certainty（explicit/contextual/uncertain）。
quoted 的 speaker 是被转述的人，不能因为配音是主角就填主角；该字段不要求额外配音或逐字对口型。
纯说明可填 []。这是关系依据，不是要把所有 source_text 都写进画面；无字图案的气泡也要保留相同归属。
用户明确指定的角色/观点关系与当前原文优先于旧 intent、摘要或人物档案。speech_turns 是可核对的
语义草案，若与原文矛盾须纠正，不能把上游错归属锁死。场景参考只给空间，不改变谁说话。
Agent 1 和 Agent 2 沿原文锚点继承全文记录；有明确依据才修正，并说明 attribution_correction。
Agent 3、4、5 沿用 Agent 2 已核对的关系，不自行再次分派台词；不确定不等于必须补出说话人。
画面若采用人物互动，讲者的问题/展示物和另一方的回应/想象分别归各自主体，气泡尾部指向对应人。
群体的多个短回应可由其中几人零散分别表达，不要求全体齐说，不把问题与不同人的回应合成主角的大气泡。
说话者也可以是没有独立角色卡的群体；稳定主体名可仍用群体名，具体几人和位置在动作中区分。
自检动作正文、图案气泡、texts.owner 和容器是否同属一个人；不能正文写主角开口、清单却填观众，
也不能把已有归属的对话偷换成“画面标注”。仅客观图例/说明可以用独立标注；不强制所有画面使用气泡。
"""

SPEECH_HANDOFF_CONTRACT = """【已确认归属的交接】
只继承本镜 semantic.speech_turns 中已核对的归属，不重新分配提问、回答或想法。
speech_mode=none、on_screen=none 的讲解不用画面人物说出；不因有配音而新增人物、气泡或开口动作。
quoted/unknown/uncertain 不等于现场对话；没有归属记录也不要求补出说话人。
关系记录不是画中文字清单；优先用既定动作、表情或图案表达，不能把原文整句抄进画面。
用户明确的本镜手动修改优先于自动归属记录。"""

SEMANTIC_CONTRACT = """【原文理解与表达目的】
先理解后选景。保留本段独有的参与者、动作/责任关系、因果来源、立场、否定和条件，
不能把具体的多重负担压成“生活压力”，不能把提问、引用的宣传或假设写成已证实事实。
intent 写观众应理解的具体含义，不写“引出话题/营造氛围/表现担忧”等空泛编辑目的，
不先选人物表情再给它找解释。不得用关键词触发固定地点或隐喻模板。
先辨认本段谁提出什么、谁如何回应、何种条件导致变化；解释性口播则辨认什么发生变化、为什么。
把具体问题、回应和反转保留到 progression_plan；intent 是这一关系的观众理解目标，不能用后半句
的结论替代整段事件。短而有信息的 intent 即可，不塞画风、构图或运镜。
每镜输出 semantic 字典：message（本段具体信息）、source_basis（覆盖关键原文的依据）、
fact_status（fact/hypothetical/metaphorical/quoted/question）、progression（相较前镜新增信息）、
continuity_requirement（原文明示的现场约束，无则空）。短语即可，不输出长篇推理。
定义与必要解释、观点与补充尽量同组；意思转折、事件或表达重点改变可在字幕边界分镜。
单个动态镜头总跨度不得超过15秒；长段可在字幕边界拆成有完整意思的镜头，不能删改配音。
Agent 0 推断的连续性只约束回到同一现场的镜头，不把全片锁在讲台、餐桌或谈话房间。
你负责语义、动静选择和分组，把具体构图交给 Agent 2。""" + SPEECH_ATTRIBUTION_CONTRACT

OBJECT_CONTINUITY_CONTRACT = """【剧情物件的连续性】
先根据全文和 continuity_rules 确认本镜物件是否是前文同一件，再描述其当前状态。
原文或既定设计已有的形状、材质、颜色、数量、大小关系，后续镜头须具体复述，不能只用物件通称代替。
持有者、位置、完整程度和加工状态可按剧情变化；变熟、变湿、被咬等变化不自动改变轮廓、花纹或种类。
不能因画风可爱而给同一物件新增动物造型、花边、装饰或配料；未指定的外观采用简洁一致的设计。
只约束本镜确实出现的物件，不强迫每镜全员、全道具出现；原文明确变形、换物或数量变化时保留变化。
支撑情节的相对尺寸和空间关系必须能从画面看出；不能把尚未解决的障碍提前画成已经解决。
"""

VISUAL_CONTRACT = OBJECT_CONTINUITY_CONTRACT + """【单张核心画面设计】
逐镜依据 source_subtitles 和 intent 设计具体可见的关系：谁对谁做什么、在什么处境；无人物内容
则展示物体、结构或机制。比较两个简短的不同表达候选，按原文依据、容易理解和便于动态展开选择。
故事可直接呈现经历，解释可用结构示意，主观想象可以有归属的气泡或叠加表达；按内容与用户画风
选择，不固定会场、主持人、气泡或情绪反转。稳健优先直接还原，增强可用比喻，均保留原文立场。
video 优先以视频前段已经开始表达内容的场面为主体，可自然充当首帧；保留主体、空间和动作余地。
允许一张完整画面容纳复合信息：主体场面清楚，辅助内容有明确归属、容器和视觉层次，就可以同时
画出随后才依次出现的必要素材。不要画多宫格或把同一实体的互斥状态直接叠在同一物理位置。
核心图无需覆盖全部过程和最后结果；之后依据原文出现的新状态、普通景物或道具可由视频提示词描述。
气泡、短字和说明图案也可以到视频对应阶段才出现，不必预先画空白气泡占位；核心图仍应有清楚的主体关系与表达。
用可见动作、处境、物体和空间关系说明含义；文字可帮助解释，但只把完整句子排成大标题不能代替设计。
控制元素靠阅读清晰度与表达必要性，不按固定数量删掉必要的角色、气泡或图解。原文中的每个名词
也不必都画出来。示意性补充可以帮助理解，不能捏造精确数字、病情、事实或人物关系。
visual_description 写一张可直接绘制的完整核心图：人物/物体及属性、正在发生的动作关系、布局、
环境、必要短字或图案及其归属。visual_design 输出 candidates（两个短方案，必须是字符串数组，例如 ["方案一的画面描述", "方案二的画面描述"]，不可用对象，每项不超过3000字）、selection_reason、
expression（narrative/experience/explanatory/metaphor/asset_display）、human_presence（none/present/partial）、
visible_evidence（观众从画面能看到的本段特有信息）。不输出内部长篇推理。
用户设定优先；同一现场的人物、服装和布局保持连续，换景不带走整套道具。人物档案不等于出镜名单。
现场参与互动的群体也是主体，不因没有姓名或独立角色卡而省略。若采用讲者与听众的互动方案，
需在 visual_description 明写可见听众所在位置、朝向及本镜反应；仅写“向观众演讲”不足以说明观众入画。
同一现场的中远景保留已建立且仍在场的群体；近景可只露局部或处于画外，空景、换景无需强行添加。
按用户设定与既定表达判断群体是否存在，不能见到“演讲”等词就套用观众模板；参考图无人不代表本镜无人。
reference_ids 按用户用途和本镜需求选择，遵守明确全程使用要求；不把全部素材当人物，也不虚称有截图。
你主导场景与表达，不编排秒级动作。后续动态导演沿着本方案展开，只为动作可执行性作局部适配。
自检画面是否体现目的、关系与信息归属是否清楚、屏幕是否可见、视线与动作是否成立，在本次输出中修正。"""

TEXT_CONTRACT = """【画面文字与归属】
允许有助于理解的短文字、对话气泡、想象气泡和必要标注，不一刀切禁字，不把完整口播复制成画中字幕。
每段短字要有明确归属和容器：谁在说、谁在想、哪件物体上的标记，或独立的画面标注。
提示词中要显示的短字用引号标明，与普通的动作或表情描述区分。
主观评价、引用、想象仍属于对应说话者或想象者，不能把气泡中的内容变成现场发生的客观事实。
表达目的、导演说明、阶段编号不是要画出的字。气泡也可以只有图案，不必强加文字。
有阶段方案时，图片以 reference_texts 为可见短字，视频以各阶段 texts 为当前可见短字。二者各自
记录原文、归属与容器，不要求内容完全相同；核心图可以收纳多个阶段的素材。保留必要短字，只禁止额外大标题、
字幕条和无关水印。无文字方案则无需添加文字。不得同时要求显示必要短字又全面禁止文字。"""

SPATIAL_MOTION_CONTRACT = """【动作位移与镜间衔接】
角色身份、场景材质和光线保持一致，不等于锁死角色坐标。原文要求行进时，写清主体相对已有地标
的起点、方向和终点；不能只写跑姿、迈腿、拍翅或转轮。固定机位下主体整体位置必须变化；只有
已选择跟拍时才以地面与背景视差证明前进，不能主体和背景都不动。原文明示原地运动则保留原地。
为动作留足画面空间，不能把主体放在行进方向的边缘又要求始终保持原构图。停车、回头、递交等
后续动作应从行进后的新位置继续，脚部落地与推进相匹配，不滑步，不循环回到开场坐标。
参考相邻镜头的已知末态与初态核对地点、左右方向、持物和在场角色；同场连续动作不凭空复位。
原文明示换景或时间跳跃时允许切换，不为了连续而改写剧情。每镜只安排时长内容得下的关键变化，
若原文过程太多，在分镜规划阶段按字幕边界拆镜；不要靠一镜内快速切换或省略关键动作硬塞进去。
"""

MOTION_CONTRACT = OBJECT_CONTINUITY_CONTRACT + SPATIAL_MOTION_CONTRACT + """【共享阶段方案 v2】
每个机制、互动或状态变化应有可辨认的初态、动作和结果，不能仅写“逐渐变化、轻微移动”。
动作幅度与速度以原文和理解需求为准，不为了动态夸大事实。运镜应揭示空间关系或变化结果，
不靠全片无目的微推代替叙事；固定镜头可以，只要动作或递进内容足够清楚。
基于 Agent2 的场景、主体和具体表达设计 motion_plan，负责把它变成可执行的过程，保持场景与
表达手段。只因动作需要调整局部站位、视线与构图，不重新选全局场景、隐喻或剧情，不迁移发言/想法归属。
按 semantic.speech_turns 与原文核对提问方、回应方和引用观点持有者；核心图中的归属应延续到动态。
scene_anchor 写稳定的场景与空间关系；participants 列本镜反复出现的主体的简短、明确名称，
可为人物、群体或物体；无固定主体可以为空。后段才出现的主体也可列入，不强制全部先画入核心图。
既定画面中可见、参与回应或承载气泡的群体必须保留到 reference_participants 与 reference_visual，
明确其位置和可见反应，不能只保留主角、把群体的回应改为主角自言自语或无归属标注。
beats 按原文推进写出开始、作用或回应、产生的变化；按本段需要组织，不强制三段式或欢乐结局。
每步有具体主体、可见动作、作用对象与反应，必要时写气泡/图案内容及切换；不能只写“体现/表示”。
通常2～4阶段，短镜头可只有1阶段；按 duration 控制变化数量，最多6阶段，不为了填满数量增加剧情。
不同提问、回应或情绪转折若先后发生，分别写出对应的当前状态，不把全过程压成一个 action 再把所有短字混在同一 texts。
不必逐字卡点。阶段不新增镜头或改变字幕、配音、时长；保持简单可执行，同一表达允许合理切镜。
每阶段 texts 列这一阶段所有可见短文字，每项含 text（精确原文）、owner（participants里的主体名，
独立示意标注用“画面标注”）、container（如对话气泡/想象气泡/物体标签，按本镜实际需要选择）。
没有文字就填[]。上一阶段短字未在当前 texts 出现即应消失，跨阶段保留的字必须再次列出。
每阶段动作须说明谁的气泡/图案出现、保留或消失；图案气泡写在 action 中，texts 仅登记实际可见的短文字（含汉字、字母、数字等）。
视频里依次展开不同阶段，不要求重现核心图里所有元素同时出现。图中没有的后续景物、动作和状态，
只要服务于原文且不虚构事实，就可在视频中描述并生成；例如泛指自然景色可用普通山水作具体示意。
reference_beat 从1开始，表示核心图主体场面接近的阶段，优先选前段有内容的场面，通常选1。
它只是时间基准，不限制核心图能包含的素材。reference_visual 保留 Agent2 单图设计的完整场面，
必须写具体可见内容，不能只填“图1”“参考图”等引用名称；目前尚未生成图片，图号不能代替画面设计。
局部适配动作；reference_participants 单列实际画入核心图的主体，是 participants 的子集。
reference_texts 独立列核心图需要画出的短字及归属（格式同 texts），无字则[]。它可包含不同阶段
的必要素材或图例说明，不必等于某一个阶段，也不是所有阶段的并集；图文归属保持明确。
只有核心图确实不画短字才填[]。如果决定让气泡留到视频生成，应同时在 reference_visual 中写清实际的无气泡场面，保留主体与构图；不要只清空文字清单却保留有字描述。
核心图用单幅连贯构图、清楚层次组织复合信息，不做多宫格，不把将来多个物理状态叠成一个实体。
例：真实环境可只有一处，人物的不同想法可以装在有归属的气泡中；这是参考素材组合，不是剧情已经同时发生。
最后让画面停在原文对应的结束状态；除非原文要求回返，不重新展示已结束的气泡或恢复核心图的全部素材。
关键表达在 duration 内完成，generation_duration 多出的尾部只自然停留；画面或气泡切换不等于
自动给每条字幕新增切口。自检来源、主体/图文归属、事件顺序、物理可执行性与结局，在本次方案内修正。"""


_LEGACY_VISUAL_FRAME = """video 优先以视频前段已经开始表达内容的场面为主体，可自然充当首帧；保留主体、空间和动作余地。
允许一张完整画面容纳复合信息：主体场面清楚，辅助内容有明确归属、容器和视觉层次，就可以同时
画出随后才依次出现的必要素材。不要画多宫格或把同一实体的互斥状态直接叠在同一物理位置。
核心图无需覆盖全部过程和最后结果；之后依据原文出现的新状态、普通景物或道具可由视频提示词描述。
气泡、短字和说明图案也可以到视频对应阶段才出现，不必预先画空白气泡占位；核心图仍应有清楚的主体关系与表达。"""

_STORY_VISUAL_FRAME = """video 的核心图是真实开场首帧：保留开场已在场的主体、空间和动作余地，只画当时可见状态。
后入场人物与后续结果不得提前出现；不为补齐身份参考将后续人物画进首帧，不画复合角色素材表或多宫格。
后续状态、普通景物、气泡与短字按原文对应阶段再出现；身份需要保持的人物须有单独真实图片依据，
未提供或未核实时保留身份输入缺口，不能因只有首帧便自由想象后续人物造型。"""

_LEGACY_MOTION_FRAME = """reference_beat 从1开始，表示核心图主体场面接近的阶段，优先选前段有内容的场面，通常选1。
它只是时间基准，不限制核心图能包含的素材。reference_visual 保留 Agent2 单图设计的完整场面，
必须写具体可见内容，不能只填“图1”“参考图”等引用名称；目前尚未生成图片，图号不能代替画面设计。
局部适配动作；reference_participants 单列实际画入核心图的主体，是 participants 的子集。
reference_texts 独立列核心图需要画出的短字及归属（格式同 texts），无字则[]。它可包含不同阶段
的必要素材或图例说明，不必等于某一个阶段，也不是所有阶段的并集；图文归属保持明确。
只有核心图确实不画短字才填[]。如果决定让气泡留到视频生成，应同时在 reference_visual 中写清实际的无气泡场面，保留主体与构图；不要只清空文字清单却保留有字描述。
核心图用单幅连贯构图、清楚层次组织复合信息，不做多宫格，不把将来多个物理状态叠成一个实体。
例：真实环境可只有一处，人物的不同想法可以装在有归属的气泡中；这是参考素材组合，不是剧情已经同时发生。"""

_STORY_MOTION_FRAME = """reference_beat 必须为1；reference_visual 描述第1阶段开场当时可见的真实首帧，不能挪用后续阶段。
目前图片尚未生成，必须写具体的开场画面设计，不能把“图1”或文字方案称为已经看过的图片证据。
reference_participants 仅列开场可见主体，是整镜 participants 的子集；后续才出现的角色不得提前入画。
reference_texts 仅列首帧已经可见的短字及归属，必须来自第1阶段 texts，无字填[]；后续文字按阶段出现。
reference_visual 与 reference_participants、reference_texts 保持一致；后续角色需要另行核对真实身份图或尾帧。
首帧只是一幅连贯开场画面，不汇总各阶段素材，不叠加未来结果，不画角色素材表或多宫格。"""


def visual_contract(context) -> str:
    """Select mutually exclusive frame semantics while retaining legacy wording."""
    if not story_uses_opening_frame(context):
        return VISUAL_CONTRACT
    text = VISUAL_CONTRACT.replace(_LEGACY_VISUAL_FRAME, _STORY_VISUAL_FRAME)
    text = text.replace('主观想象可以有归属的气泡或叠加表达', '故事的主观想象通过独立镜头和前后关系表达')
    return text + story_frame_contract(context) + SPATIAL_MOTION_CONTRACT


def motion_contract(context) -> str:
    if not story_uses_opening_frame(context):
        return MOTION_CONTRACT
    return MOTION_CONTRACT.replace(_LEGACY_MOTION_FRAME, _STORY_MOTION_FRAME) + story_frame_contract(context)


def frame_text_contract(context, selected_contract: str) -> str:
    if not story_uses_opening_frame(context):
        return selected_contract
    return selected_contract.replace('核心图可以收纳多个阶段的素材。', '动态镜头核心图只保留开场已可见的素材。') + (
        '\n故事路线默认不采用以上图解气泡与叠加手法；心理或回忆通过独立实体镜头表达。'
        '只有原文明示这种可见形式时才保留，不把“角色在想”当作画人物气泡的要求。')
