"""Story-first H3 dialogue projects; no synthetic narration job or audio file."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .h3_dialogue import DialoguePlan, Turn, validate_dialogue_draft
from .video_cast_planning import bind_planned_characters, plan_reference_ids
from .video_plan import normalize_shots, planning_fingerprint
from .story_workspace import StorySource


from .video_render_options import VideoRenderOptions


class NativeStoryCreate(VideoRenderOptions):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    name: str = Field(min_length=1, max_length=100)
    story: str = Field(min_length=20, max_length=16000)
    speech_mode: Literal['dialogue', 'mixed']
    dialogue_route: Literal['h3_native','external_tts'] = 'h3_native'
    dialogue_tts_engine: Literal['indextts25','qwen3'] = 'indextts25'
    style: str = Field(min_length=1, max_length=10000)
    characters: str = Field(default='', max_length=10000)
    world: str = Field(default='', max_length=10000)
    ratio: Literal['16:9', '9:16'] = '16:9'
    duration_seconds: int | None = Field(default=60, ge=16, le=600, strict=True)
    minimum_shots: int | None = Field(default=6, ge=2, le=40, strict=True)
    visual_pacing_preset: Literal['auto', 'slow', 'standard', 'fast', 'custom'] = 'auto'
    visual_min_duration: float = Field(default=5, ge=4, le=15)
    visual_target_duration: float = Field(default=8, ge=5, le=15)
    visual_max_duration: float = Field(default=12, ge=6, le=15)
    visual_max_slides: int = Field(default=6, ge=1, le=12)
    comfyui_profile_id: str = Field(min_length=1, max_length=80)
    image_profile_id: str = Field(default='', max_length=80)
    image_resolution: Literal['1k', '2k', '4k'] = '1k'
    use_cloud_image_pool: bool = False
    scene_references_enabled: bool = True
    one_click: bool = True
    story_source: StorySource | None = None


class DialogueEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1, strict=True)
    turns: list[Turn] = Field(max_length=6)


def edit_dialogue(owner, identity, shot_id, data):
    from . import video_studio as studio
    with studio.LOCK:
        path = studio.directory(int(owner), identity)
        record = studio.read(path)
        studio.editable(record, data.revision)
        shot = studio._find_shot(record, shot_id)
        if not record.get('native_story') or not shot.get('native_dialogue'):
            raise HTTPException(400, '本镜不是 H3 原生对话镜头')
        candidate = copy.deepcopy(shot['native_dialogue'])
        candidate['turns'] = [row.model_dump() for row in data.turns]
        # A changed line/speaker may alter reactions even when its times match.
        # Rebuild only the timed actions on the next explicit generate request.
        def performance_basis(turns):
            return [{k:v for k,v in turn.items() if k not in ('emotion','emotion_strength')} for turn in turns]
        if performance_basis(candidate['turns']) != performance_basis(shot['native_dialogue']['turns']):
            candidate.pop('performance_beats', None)
        try:
            candidate = validate_dialogue_draft(candidate)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if candidate == shot['native_dialogue']:
            return record
        # A failed HTTP response is not proof the GPU rejected a paid request.
        # Preserve the accepted identity until the original task is terminal.
        uncertain = (shot.get('video_task_id') or shot.get('video_request') or shot.get('video_execution_started'))
        if uncertain and shot.get('video_status') != 'completed' and not shot.get('video_terminal'):
            raise HTTPException(409, '本镜上次视频请求尚未确认结束，请先继续查询原任务，再修改台词')
        shot.setdefault('dialogue_history', []).append(copy.deepcopy(shot['native_dialogue']))
        shot['dialogue_history'] = shot['dialogue_history'][-20:]
        studio._invalidate_shot_video(record, shot, '本镜台词或说话时序已修改')
        shot['native_dialogue'] = candidate
        shot.pop('dialogue_stale', None)
        shot.pop('h3_prompt', None)
        shot.pop('h3_prompt_source', None)
        if record['status'] in studio.VIDEO_STAGE_STATUSES:
            record['status'] = 'video_review'
        record.pop('export', None)
        record.update(revision=record['revision']+1, error='')
        record.setdefault('logs', []).append(f'{shot_id}：已保存台词；本镜核心图和其他镜头保持，需重新生成本镜视频。')
        # The old whole-story candidate must not overwrite this manual revision.
        record.pop('native_planning_state', None)
        studio.save(path, record)
        return record


def create_for_user(data: NativeStoryCreate, user):
    from . import video_studio as studio, one_click
    from .comfyui_bridge import video_profile
    owner = int(user['id'])
    creation_basis = data.model_dump()
    # Older clients did not send this field. Keep their retry identity stable
    # so a deployment cannot strand an already-created project after a lost reply.
    if 'scene_references_enabled' not in data.model_fields_set:
        creation_basis.pop('scene_references_enabled')
    if 'dialogue_route' not in data.model_fields_set:
        creation_basis.pop('dialogue_route')
    if 'dialogue_tts_engine' not in data.model_fields_set:
        creation_basis.pop('dialogue_tts_engine')
    from .video_pacing import PACING_FIELDS, pacing_policy
    for key in ('comfyui_steps', 'comfyui_width', 'comfyui_height', *PACING_FIELDS):
        if key not in data.model_fields_set:
            creation_basis.pop(key, None)
    fingerprint = _hash(creation_basis)
    # Client-generated identity makes retries after a lost response idempotent.
    # No existing unrelated project, even under the same owner, may be adopted.
    path = studio.directory(owner, data.request_id)
    with studio.LOCK:
        if (path / 'record.json').is_file():
            record = studio.read(path, include_archived=True)
            if record.get('native_creation_sha256') != fingerprint:
                raise HTTPException(409, '创建标识已用于另一份文稿或设置，请新建草稿')
            if record.get('archived'):
                raise HTTPException(409, '这份草稿已归档，请恢复原项目')
            if data.one_click and one_click.load(owner, record['id']) is None:
                one_click.register_native(owner, record)
            return record
        try:
            profile = video_profile(owner, data.comfyui_profile_id)
            if profile.get('video_input_mode') != 'composition_reference':
                raise ValueError('角色音色对白需要支持音频参考的 H3 多参工作流')
            from .comfyui_bridge import _patch_voice_references, _api_graph, WorkflowMappings
            _patch_voice_references(copy.deepcopy(_api_graph(profile['workflow'])),
                WorkflowMappings.model_validate(profile.get('mappings') or {}), ['preflight.wav'])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        params = dict(dynamic_video=True, speech_mode=data.speech_mode,dialogue_route=data.dialogue_route,
                      dialogue_tts_engine=data.dialogue_tts_engine,
            dynamic_auto_advance=data.one_click,
            dynamic_execution_owner='server' if data.one_click else 'browser',
            # A one-click run must inspect its generated assets before export.
            # Manual/browser runs keep the legacy opt-in review behavior.
            quality_review_policy='media' if data.one_click else 'standard',
            auto_character_design=True, dynamic_text_mode='visual_first',
            scene_references_enabled=data.scene_references_enabled, video_generation_backend='comfyui',
            comfyui_profile_id=data.comfyui_profile_id, comfyui_h3_prompt_agent=True,
            comfyui_reference_audio=False, image_profile_id=data.image_profile_id,
            image_resolution=data.image_resolution, use_cloud_image_pool=data.use_cloud_image_pool,
            video_orientation='portrait' if data.ratio == '9:16' else 'landscape',
            content_mode='general')
        from .video_render_options import snapshot as render_snapshot, apply as apply_render_options
        params.update(render_snapshot(data.model_dump()))
        if any(key in data.model_fields_set for key in PACING_FIELDS):
            params.update({key: getattr(data, key) for key in PACING_FIELDS})
        try:
            apply_render_options(profile, params)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if data.story_source is not None:
            from .story_workspace import resolve_story_source
            params['story_source'] = data.story_source.model_dump(exclude_none=True)
            params['story_snapshot'] = resolve_story_source(owner, params['story_source'], data.story)
        record = dict(id=data.request_id, revision=1, status='draft', shots=[], references=[],
            source_images=[], logs=[], error='', native_creation_sha256=fingerprint, native_owner_id=owner,
            native_story={'text':data.story, 'duration_seconds':data.duration_seconds,
                          'minimum_shots':data.minimum_shots},
            scenes=[dict(slide_id='story_001', start=0, end=data.duration_seconds or 16, text=data.story)],
            settings=dict(name=data.name, style=data.style, characters=data.characters,
                          world=data.world, ratio=data.ratio, dynamic_text_mode='visual_first'),
            creation_parameters=params)
        if any(key in data.model_fields_set for key in PACING_FIELDS):
            record['native_story']['visual_pacing'] = pacing_policy(params)
        if data.speech_mode == 'mixed':
            record['native_narrator'] = {'id':'pc_' + uuid.uuid4().hex}
        studio.save(path, record)
        if data.one_click:
            one_click.register_native(owner, record)
        return record


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


NATIVE_DIRECTOR_REVISION = 'native-performance-timeline-v23-audio-route-pacing'
NATIVE_DIRECTOR = '''你是多人故事短片导演。按给定原文和独立角色ID制作连续、有因果的完整故事，不能只做技术对白测试。
native_dialogue.duration必须是4到15的整数秒；台词turns和动作performance_beats的起止时间可用小数秒。不得因整数镜长增加事件或拉长台词，仅在必要时留不足一秒的结束状态保持。
保留人物动机、事件顺序和结局，已有直接引语逐字保留；需将叙述转为对白时写短而自然的中文，不添加原文没有的事实。
dialogue 只能角色说话；mixed 须有独立画外旁白和角色对白，不能把角色的话都交给旁白读。
角色发言路线须有至少一名角色说出实际台词，发言者依据采用稿确定；允许只有一名角色发言，不为凑多人对话给静默倾听者编台词，也不用喷嚏、笑声、叹气冒充另一名发言者。不因单人发言合并掉有叙事作用的听者反打和反应镜头。
按dialogue_route区分制作机制：h3_native时H3生成角色对白，Qwen提供固定音色参考；external_tts时所选配音引擎先合成准确台词，程序按实测录音调整时序，H3按角色音频指导生成对应嘴部动作。external_tts中的旁白单独后期合成，不传给H3作为角色口型或音色参考。每镜4至15秒，每镜最多3个发声者（含旁白），全片不受此限制。
每句台词预留呼吸和反应时间，依次发言不重叠，按每秒约3至4个中文字分配，不塞长句。
先按实际台词字数估计发言时间，再安排必要动作和短停顿，最后决定镜长；15秒是上限，不是默认目标。说话时可并行的看向、点头、简单手势不另加一段等待；只对必须独立完成的动作分配额外时间。无叙事作用的前后凝视、端详和等待不用于凑时长。超过目标镜长时按完整语义或动作边界拆镜，不靠加快语速硬塞。
以H3能直接表现的物理事件设计：人物内心、比喻、愿望和未来事件不自动转为可见头像、分身、气泡或回忆画面。台词提到的人或物不等于本镜出场对象；只用现有角色的一个自然表情承接内心，除非原稿明确要求独立可见场面。
全故事道具目录只记录跨阶段不变的身份和材质，不写“以后烤熟/长成/装饰后”的外观；阶段性形状、颜色、完整程度和装饰必须归入各镜continuity_states。每镜动作只连接本镜开场与结束状态，不提前实现未来结果。
原稿明确写“已准备好、已涂好、已经完成”等条件时，把它视为该动作开始前的既有事实，不擅自改成未完成，更不新增准备工序来填镜长。原稿未交代更早状态时不要编造相反的初始状态，导致后面被迫新增操作；检查状态轨迹与原文的这些条件一致。保留原稿明确需要演出的变化，不能用此规则提前完成故事事件。
按剧情节拍和观看重点分镜，不机械地每段或每位说话人切一镜。同场景对话先设计可剪辑的镜头覆盖：用建立镜头交代关系，重要发言、反问与转折优先说话者中近景或过肩反打，听者的反应有叙事作用时给反应镜头；关键物件确实承载发现或决定时才给物件近景。短问答可以同镜，但不能因此把全段交谈都合并为同一宽景、同一机位或每镜全员围桌展示。每镜有明确视觉重点，其他在场者可在画外或以肩背作前景，不能因收紧取景删掉他们的既定对白。需要反打时在shots之间切换机位，不要求H3在一个连续镜头内自行切镜。保持人物物理站位、视线方向和对话轴线，换机位不等于人物移动；道具状态继续跨镜继承。镜数依据表达需要，不把最低镜数当成固定总数；也不为凑反打制造无意义的四秒反应等待。原稿明确要求长镜头或多人同框互动时按其要求保留。
必要的递接物体、行走或反应镜头允许turns=[]，仅有自然环境和动作声，不为填字段添加台词。
无人且无旁白的空镜允许speakers=[]、turns=[]；有台词时仍必须声明对应说话者，不能为了填speakers凭空增加人物或旁白。
每镜一个空间和主要动作；转场、回忆和复杂多人交接拆镜。区分开场可见与稍后入画。
只增加支撑剧情、人物辨识和动作衔接的约束。原文未指定、动作也不依赖具体左/右手时，只写持物者和持物关系，不擅自锁定某只手或爪；若必须指定，所有中英文状态与动作统一，并注明角色自身左右或画面左右。已有明确批准的约束仍需保留，不可为通过检查而删除。
说话时优先一个清楚可见的表情或简单动作；复杂递接、转身、位移安排在发言前后，给出足够执行时间，不让同一角色边长句发言边完成多项复杂动作。
搭肩、倚靠、牵连等持续接触也有结束动作：下一次走位或第三人进入接触者之间之前，先安排相关部位抬离、松开或让出通路，不让模型自行猜测如何穿过身体。
speaker.presence=opening表示开场可见；later表示本镜稍后才进入画面或从遮挡后露面。
后入画者仍绑定身份图，position明确初始位置，performance_beats明确入画路径或遮挡解除以及开始说话的时机。
image_prompt只画presence=opening的角色；later角色不得提前画在门外、门上或空中，但可描述遮挡物。
若开场的照片、画作或屏幕中呈现项目角色，在该镜独立提供depicted_characters:[{project_character_id,basis}]，basis说明具体载体与原文依据；没有则[]。这些是静态影像身份参考，不是真人入场，不加入speakers、不会说话或获得音色。真人同时在场时只在speakers声明一次，图中描述其静态影像即可。每个image_prompt的图号必须对应本镜真人开场或depicted_characters实际绑定的参考，不能只写图号却不声明身份。
不要编造分身、人物气泡或把定妆图放进成片。旁白没有可见身体、不占画面位置。
每个source_text是原文连续片段，全部片段依次连接必须完整覆盖原文，不能删字、换词或重复。
image_prompt中文，只描述本镜真实开场、构图、画风、实际可见角色和物体；不画后续动作拼图，不画台词文字。
景别须围绕本镜叙事主体定义，不能仅写“桌子的中景”或“房间近景”。人物对话镜头在opening、image_prompt和scene一致说明谁是视觉重点、人物可见到哪个身体部位、为读懂本镜必须入画的道具或接触部位；按该镜已有动作决定范围，不一律套用半身或全身。反打须在scene说明相机位于哪一方附近、朝向哪一方，以及主体身后实际可见的背景；仅写“反打”或“保持轴线”不够。由既定物理站位与本镜机位推导重要人物的画面左右、前后景，以及开场头部朝向、视线目标及其画内或画外方向，不因换机位让人物交换物理站位。低头后抬眼等变化留在动作段，不提前画成结束姿态。背景地标用于保持空间关系，不要求全部完整入画；相机身后或取景外的地标不列入可见背景。说明哪些非必要区域可在画外，不为收紧取景删除剧情要求可见的空座、手中物或动作通路。先核对这些要求能同时满足，再提交分镜；不能把互相冲突的取景要求留给生图模型解决。
image_prompt的图片标签必须使用给定reference.label，按角色对应，不把无关角色全传入。
对跨镜承载关键事件的道具，先在顶层props中设计一次固定外观与结构，再在各镜props中引用同一id；没有此类道具返回props=[]。
固定颜色、形状、材质和结构必须支持原稿操作，不添加会阻止既定操作的紧固、连线、封闭或承托条件；不要给每镜另编造型。
固定外观不包含剧情中的临时位置、开合、遮挡、损坏和占用状态，这些变化仍由各镜opening/action/performance_beats准确描述。
为每个props另给顶层prop_state_tracks，分别列出initial_state和所有会用到的states，每个state含id、description（中文）、description_en（英文）。
状态描述写清物件是否已存在、相对固定地标的位置、持有者及开合/完整状态；左右必须注明观察方向，机位变化不能改变物理位置。
随角色移动的手持物优先用持有者、接触关系和朝向描述位置；普通手持物不随每次机位或所在地点变化另建地理状态。只有原稿确实依赖某个固定地标时才把它写进状态；人物行走路径和场景转场仍在镜头开场与动作中写清，不能省略必要移动。放下、递给别人、改变开合或持物姿态仍须定义对应状态。
每个state是某一时刻的静态快照，不写“随后走向、接着递给、将会”等动作过程或未来状态。不同位置、持有者和开合状态分别定义；连接它们的动作放进performance_beats和change_basis，不在开场状态提前写出结束结果。
不要等旁白或台词提到一个原本已存在的物件才把它放入世界；本镜没操作它不等于看不到它。物件不在取景内须有具体取景/遮挡依据。
每镜prop_timeline必须覆盖全部props，每项含prop_id、opening_state、ending_state、visibility（opening/later/offscreen）、visibility_basis、change_basis。
opening_state严格继承上一镜ending_state，首镜继承initial_state；镜外也保留物理状态。没有安装、移动、递接、开合等动作不能换状态。
change_basis只解释opening_state与ending_state之间的净变化：两个ID不同时，逐字引用本镜source_text或action的一个连续片段，不拼接多段、不改写。两个ID相同时填写空字符串，即使镜内有举起再放回、打开再合上等暂时变化，也保留完整action/performance_beats，只把change_basis留空，不能为通过校验而删掉动作。先定跨镜空间关系，再写各镜图像与动作。
用不同状态承接已批准的移位或变化，不能把物件永远锁在最初位置。时空转场/回忆引起状态变化也必须有明确的本镜原稿依据。
道具basis逐字引用原文连续片段。每镜只引用实际可见或稍后出现的道具，presence=opening或later；未使用道具不添加进画面。
appearance用中文，appearance_en用英文具体说明同一道具的稳定外观。程序会将该描述传入对应核心图和H3场景，不把道具当作说话角色或给它音色。
intent、action、opening中文；native_dialogue的scene、soundscape、position、appearance、voice_description和delivery严格英文。
每句turn同时给出emotion（neutral/happy/angry/sad/afraid/disgusted/melancholic/surprised/calm）与emotion_strength（0到1，默认0.35）。只按本句已确定的表演要求选择，不根据疑问标点或角色年龄猜测；情绪不明确用neutral，不把整镜或整故事情绪套到每一句。不添加额外台词，保持角色已采用音色。
native_dialogue描述一个连续镜头，scene只写固定场景、机位和开场空间关系；可见动作与空间变化全部写入performance_beats。台词仅在turns.text保留中文，不在英文动作中再抄台词。
performance_beats按开场、发言期间、发言结束后的顺序描述一个主要动作，明确谁先说、谁在听及说完后的动作结果，与turns时间一致。
原稿已批准的持续表情或尚未解除的情绪，要在performance_beats中明确承接到发言期间、说完后的停顿及尾帧，不能只写开头。只有原稿明确的情绪转折才改变；按已有表演依据写可见表现，不按emotion标签机械生成面部动作，也不自行增加微笑、释然或情绪反转。
performance_beats只交代本镜可见动作和可听的画外事件；不写明确不会入画也不会发声的画外准备过程，相关信息留在跨镜连续性状态。不能让后续角色通过一段“他在画外准备且不入画”的表演描述提前进入本镜。实际入场及必要的画外对白、声音按原稿保留。
不要在scene或performance_beats反复写角色外貌、音色或禁止说话的规则；外貌只放appearance，位置只放position，具体台词只放turns。
position主要描述站位/入场位置，不再复制整段动作；中文action与performance_beats表达同一事件顺序。稳定道具外观和状态由程序附加，不在scene再抄整份目录。
同一连续空间先确定可复用的固定地标关系，再写角色position；多人围桌等构图不能仅写“在桌边/另一个位置”，须足以区分各角色的实际位置。用窗侧、门侧等已设计地标或人物相对关系描述，不凭空增加家具；机位改变时画面左右可以变化，物理站位不能无动作交换。
scene中的固定布景与装饰在同一连续场景保持；后续镜头未提及不等于消失。按真实取景保留可见部分，特写不强求展示整个背景。不要为说明背景事实临时添加只在一镜存在的标牌或文字；若确需场景内文字，写明载体与可见镜头，“不生成字幕”不能改写成禁止该场景内文字。
native_dialogue必须提供performance_beats：按顺序连续覆盖0到duration，每句turn的完整[start,end]恰好一个{start,end,action,action_zh}；无对白的时间间隙可细分为多个连续动作段，不漏段、不重叠、不跨越台词边界，不拆分同一句发言。
action英文、action_zh中文，两者表达同一具体动作，用角色姓名的英文称谓清楚区分实际人物；不含台词、音色或口型禁令。台词由程序精确放进对应节拍。
发言期间写说话者面向谁、听者做什么；安静间隙写移动路径、接触关系和停止位置。说完才走的动作只能在发言结束后的节拍，不提前或循环。
turns=[]时仅一段0至duration动作。
所有节拍动作合计应简洁，按镜头复杂度约80至180个英文词；不能为凑字数添加动作。不强制用户填写时间表，导演负责自动生成。
speaker.character_id用项目提供ID，不造ID；name照抄项目称呼；可见角色picture从2起依次编号，voice_id=null等用户采用。
旁白kind=narrator、picture=null、on_screen=false、position=""；可见角色kind=character、on_screen=true。
不需要发言的听者也在speakers中列出，但没有turn，不为其添加音频。英文soundscape写实际环境/动作声，不加音乐。
返回JSON {"props":[{"id":"prop_01","name":"道具名称","basis":"原文连续片段","appearance":"固定外观和结构","appearance_en":"Stable appearance and structure."}],
"prop_state_tracks":[{"prop_id":"prop_01","initial_state":"state_initial","states":[{"id":"state_initial","description":"从已定义观察方向看，物件在某固定地标旁，完整且尚未移动。","description_en":"The object remains intact at its established location beside a fixed landmark."}]}],
"shots":[{"source_text":"原文连续片段","intent":"本镜作用","action":"动作与反应","props":[{"id":"prop_01","presence":"opening"}],
"prop_timeline":[{"prop_id":"prop_01","opening_state":"state_initial","ending_state":"state_initial","visibility":"opening","visibility_basis":"物件位于本镜实际可见的空间。","change_basis":""}],
"opening":"真实开场画面","image_prompt":"完整开场制图提示词",
"native_dialogue":{"duration":4,"scene":"A fixed view of the room and its table.","soundscape":"Quiet room ambience.",
"speakers":[{"character_id":"pc_ID","name":"称呼","picture":2,"position":"left of the table",
"appearance":"English appearance","voice_description":"","kind":"character","on_screen":true,"presence":"opening","voice_id":null}],
"turns":[{"character_id":"pc_ID","text":"中文短句。","start":0.5,"end":3.5,"delivery":"naturally","emotion":"neutral","emotion_strength":0.35}],
"performance_beats":[{"start":0,"end":0.5,"action":"The character looks toward the table.","action_zh":"角色看向桌子。"},
{"start":0.5,"end":3.5,"action":"The character indicates the table while speaking.","action_zh":"角色说话时示意桌子。"},
{"start":3.5,"end":4,"action":"The character lowers its arm.","action_zh":"角色放下手臂。"}]}}]}。
minimum_shots为null时，按完整动作、台词轮次与场景转换决定镜头数；非空时才作为镜头数量下限。
duration_seconds为null时，按自然中文语速、动作耗时与必要停顿决定总时长；非空时作为期望时长，不得挤压对白。
visual_pacing为null或缺失时自主安排节奏；有值时参考target_duration与max_duration在动作节拍切镜，完整保留每句台词及说话者。不得为凑镜数或秒数重复事件、加词或删掉原故事内容。
'''


from .video_timing_policy import TIMING_POLICY
NATIVE_DIRECTOR += TIMING_POLICY


class _DirectorDialogueDraft(DialoguePlan):
    """Only the director parsing boundary accepts a fractional shot duration."""
    duration: float = Field(ge=4, le=15, strict=True, allow_inf_nan=False)


def normalize_director_timing(value):
    """Validate the original schedule first, then pad only its ending pose.

    Submitted plans and manual edits still use the strict integer contract.
    A malformed original schedule must not become valid just by adding time.
    """
    if not isinstance(value, dict) or type(value.get('duration')) is not float:
        return value
    original = _DirectorDialogueDraft.model_validate(value, context={'unbound_draft': True}).model_dump()
    duration = original['duration']
    target = math.ceil(duration)
    padded = copy.deepcopy(original)
    padded['duration'] = target
    beats = padded.get('performance_beats')
    if duration < target and beats:
        if len(beats) >= 13:
            raise ValueError('导演镜长含小数且动作段已达上限，不能安全补结束留白；请按整数镜长规划')
        beats.append(dict(start=duration, end=target,
            action='Maintain the established ending pose and object state.',
            action_zh='保持既定结束姿态与物件状态。'))
    # Re-run the normal contract after padding; speaker/text/actions untouched.
    return validate_dialogue_draft(padded)


def compile_storyboard(raw, record, cast, references):
    """Validate the full story and stable identities before any shot image work."""
    story = record['native_story']
    rows = raw.get('shots') if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not (story.get('minimum_shots') or 1) <= len(rows) <= 100:
        raise ValueError('对话分镜数量不符合本次故事设置')
    from . import video_prop_continuity as prop_continuity
    props = prop_continuity.catalog(raw.get('props', []), story['text'])
    from . import video_prop_states as prop_states
    timeline = prop_states.compile_timeline(raw, props, rows)
    catalog = {row['project_character_id']: row for row in cast['characters']}
    narrator = (record.get('native_narrator') or {}).get('id')
    scenes, shots, sources, spoken = [], [], [], set()
    clock = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError('对话分镜格式无效')
        for key in ('source_text', 'intent', 'action', 'opening', 'image_prompt'):
            if not isinstance(row.get(key), str) or not row[key].strip() or len(row[key]) > 20000:
                raise ValueError('对话分镜缺少原文、画面或动作依据')
        sources.append(row['source_text'])
        plan = validate_dialogue_draft(normalize_director_timing(row.get('native_dialogue')))
        declarations, names, opening_names = [], [], []
        picture = 2
        for speaker in plan['speakers']:
            identity = speaker['character_id']
            speaker['voice_id'] = None  # A director can never adopt a user's saved voice.
            if speaker['kind'] == 'narrator':
                if not narrator or identity != narrator:
                    raise ValueError('对白路线不能加入旁白，混合路线须使用本项目旁白ID')
                speaker['name'] = '旁白'
            else:
                if identity not in catalog:
                    raise ValueError('对白分镜使用了未绑定的角色身份')
                if not speaker['on_screen']:
                    raise ValueError('本镜角色在画外，请拆分为清楚的说话者镜头；不要伪造开场出镜')
                speaker.update(name=catalog[identity]['name'], picture=picture)
                picture += 1
                names.append(speaker['name'])
                presence = speaker.get('presence', 'opening')
                if presence == 'opening':
                    opening_names.append(speaker['name'])
                declarations.append(dict(project_character_id=identity, presence=presence,
                    basis=row['opening'] if presence == 'opening' else row['action']))
        spoken.update(turn['character_id'] for turn in plan['turns'])
        scene_id = f'scene_{index:03d}'
        scenes.append(dict(slide_id=scene_id, start=clock, end=clock+plan['duration'], text=row['source_text']))
        clock += plan['duration']
        depicted = row.get('depicted_characters', [])
        if not isinstance(depicted, list):
            raise ValueError(f'shot_{index:03d} 静态影像角色须为数组')
        physical = {entry['project_character_id'] for entry in declarations}
        depicted_seen = set()
        for entry in depicted:
            if (not isinstance(entry, dict) or set(entry) != {'project_character_id', 'basis'}
                    or not isinstance(entry.get('project_character_id'), str)
                    or entry['project_character_id'] not in catalog
                    or entry['project_character_id'] in depicted_seen
                    or not isinstance(entry.get('basis'), str) or not entry['basis'].strip()
                    or len(entry['basis']) > 2000):
                raise ValueError(f'shot_{index:03d} 静态影像身份声明无效或重复')
            depicted_seen.add(entry['project_character_id'])
            # The same identity may appear in a photograph and beside it.
            # Retain the physical entrance plan, sharing its identity reference;
            # only an otherwise absent identity needs a depiction-only entry.
            if entry['project_character_id'] in physical:
                if any(d['project_character_id'] == entry['project_character_id'] and d['presence'] == 'later'
                       for d in declarations):
                    raise ValueError(f'shot_{index:03d} 后入场角色与开场静态影像同时出现，须分开规划以避免提前入场')
                continue
            declarations.append({**entry, 'presence': 'depicted'})
        cast_plan = bind_planned_characters(cast, declarations)
        refs = plan_reference_ids(cast, cast_plan, [])
        labels = {ref.get('label'): ref['id'] for ref in references}
        mentioned = {f'图{n}' for n in re.findall(r'图\s*([1-9]\d*)(?!\d)', row['image_prompt'])}
        if any(labels.get(label) not in refs for label in mentioned):
            raise ValueError(f'shot_{index:03d} 核心图提示词引用了未绑定的角色图号；请声明实际开场角色或静态影像身份，不能让图片模型猜测')
        shots.append(dict(id=f'shot_{index:03d}', slide_ids=[scene_id], kind='video',
            intent=row['intent'], action=row['action'], image_prompt=row['image_prompt'],
            visual_description=row['opening'], video_prompt='图1为本镜构图和画风参考。' + row['action'],
            native_dialogue=plan, cast_plan=cast_plan, reference_ids=refs,
            motion_plan=dict(version=2, scene_anchor=row['opening'], participants=names,
                reference_participants=opening_names, reference_beat=1, reference_visual=row['opening'],
                reference_texts=[], beats=[dict(action=row['action'], texts=[])])))
        bindings = row.get('props', []) if timeline is None else [
            {'id': state['prop_id'], 'presence': state['visibility']}
            for state in timeline[index - 1] if state['visibility'] != 'offscreen']
        shots[-1] = prop_continuity.apply(shots[-1], prop_continuity.bind(props, bindings))
        if timeline is not None:
            shots[-1] = prop_states.apply(shots[-1], timeline[index - 1])
    compact = lambda value: ''.join(value.split())
    if compact(''.join(sources)) != compact(story['text']):
        raise ValueError('对话分镜没有按原文顺序完整覆盖故事，请修订分镜，不能默默删改采用稿')
    character_speakers = (spoken & catalog.keys()) - {narrator}
    if narrator:
        if narrator not in spoken or not character_speakers:
            raise ValueError('混合故事必须同时包含旁白和角色实际台词')
    elif not character_speakers:
        raise ValueError('对话故事至少需要一名实际发言的角色')
    return scenes, normalize_shots(shots, scenes, [ref['id'] for ref in references])


def _repair_request(state, image_reports=()):
    from .video_dialogue_story_review import partition_problems
    story_problems, advisory = partition_problems(state.get('story_review'), strict=state.get('strict_story_review', False))
    failed = {r['id'] for r in state.get('review', []) if r.get('problems')}
    for problem in story_problems:
        failed.update(problem['shot_ids'])
    # A continuity failure can be caused by an earlier shot's end state.
    # Permit only explicitly cited, existing dependencies to be repaired too;
    # otherwise the director is forced to preserve the contradictory setup.
    candidate = state.get('candidate')
    candidate_rows = candidate.get('shots') if isinstance(candidate, dict) else None
    if not isinstance(candidate_rows, list):
        previous = (state.get('repair') or {}).get('previous')
        return {'previous': copy.deepcopy(previous), 'problems': state.get('error', ''),
                'shot_ids': [], 'story_problems': [], 'staging_problems': []}
    available = {f'shot_{i:03d}' for i in range(1, len(candidate_rows) + 1)}
    failed.update(set(state.get('validation_shot_ids', [])) & available)
    related = set()
    for row in state.get('review', []):
        for problem in row.get('problems', []):
            if problem.get('code') == 'opening_conflict':
                text = str(problem.get('evidence', '')) + ' ' + str(problem.get('repair', ''))
                related.update(re.findall(r'(?<![A-Za-z0-9_])shot_\d{3}(?![A-Za-z0-9_])', text))
    failed.update(related & available)
    image_problems = []
    for report in image_reports:
        issues = report['review']['issues']
        failed.update(issue['shot_id'] for issue in issues if issue['shot_id'] in available)
        pairs = [row for row in report['review'].get('continuity_checks', [])
                 if any(row[key]['status'] == 'conflict' for key in ('characters', 'setting'))]
        for pair in pairs:
            failed.update({pair['from_shot_id'], pair['to_shot_id']} & available)
        image_problems.append(dict(fingerprint=report['fingerprint'],
            image_versions=copy.deepcopy(report['image_versions']), issues=copy.deepcopy(issues),
            continuity_checks=copy.deepcopy(pairs)))
    summary = state.get('error', '')
    if advisory and state.get('review') is not None:
        # The prior error may also contain editorial advice. Do not leak those
        # discarded requests back into the director through its summary.
        summary = json.dumps({'story_problems':story_problems,
            'staging_problems':[r for r in state.get('review', []) if r.get('problems')]}, ensure_ascii=False)
    return {'previous':copy.deepcopy(state['candidate']), 'problems':summary, 'shot_ids':sorted(failed),
            'image_problems': image_problems,
            'story_problems':story_problems,
            'staging_problems':copy.deepcopy([r for r in state.get('review', []) if r.get('problems')])}


def _remember_staging_review(state):
    if not state.get('candidate') or not state.get('review'):
        return
    entry = {'candidate_sha256': _hash(state['candidate']),
             'shot_sources': [{'id': f'shot_{i:03d}', 'source_text': row.get('source_text', '')}
                              for i, row in enumerate(state['candidate'].get('shots', []), 1)],
             'review': copy.deepcopy(state['review'])}
    history = state.setdefault('staging_review_history', [])
    if not history or history[-1] != entry:
        history.append(entry)
    state['staging_review_history'] = history[-3:]


def _staging_context(context, state):
    result = copy.deepcopy(context)
    if state.get('staging_review_history'):
        result['staging_review_history'] = copy.deepcopy(state['staging_review_history'])
    repair = state.get('repair') or {}
    if repair.get('problems') and isinstance(repair.get('previous'), dict):
        result['previous_staging_repair'] = {
            'problems': repair['problems'],
            'shot_sources': [{'id': f'shot_{i:03d}', 'source_text': row.get('source_text', '')}
                             for i, row in enumerate(repair['previous'].get('shots', []), 1)]}
    if repair.get('image_problems'):
        result['previous_core_image_findings'] = copy.deepcopy(repair['image_problems'])
    return result


def _resume_structurally_valid_candidate(record, state):
    """Recheck saved inputs after a compatible validator fix, without an LLM."""
    if not state.get('fingerprint') or 'review' in state or 'story_review' in state:
        return False
    from .video_studio import planning_parameters, planning_references
    from .video_dialogue_story_review import REVISION as STORY_REVIEW_REVISION
    parameters, references = planning_parameters(record), planning_references(record)
    cast = parameters.get('project_cast')
    if not cast:
        return False
    basis = dict(story=record['native_story'], speech_mode=parameters['speech_mode'],
        cast=cast, narrator=record.get('native_narrator'), settings=record['settings'], references=references)
    fingerprint = lambda revision: _hash({**basis, 'story_review_revision': STORY_REVIEW_REVISION,
                                           'native_director_revision': revision})
    if state['fingerprint'] not in {fingerprint(NATIVE_DIRECTOR_REVISION),
                                  fingerprint('native-performance-timeline-v9-held-prop-relations')}:
        return False
    candidate = state['candidate']
    if not isinstance(candidate, dict):
        return False
    try:
        if candidate.get('props') and 'prop_state_tracks' not in candidate:
            return False
        _, shots = compile_storyboard(candidate, record, cast, references)
        if any(not shot['native_dialogue'].get('performance_beats') for shot in shots):
            return False
    except ValueError:
        return False
    state.update(fingerprint=fingerprint(NATIVE_DIRECTOR_REVISION), attempt=0)
    for key in ('error', 'repair', 'validation_shot_ids'):
        state.pop(key, None)
    # The saved candidate still goes through both semantic reviews. This is
    # neither approval nor permission to skip identity/voice/image checks.
    return True


def retry_failed_plan(record, image_reports=()):
    """Explicit website retry gets a new bounded repair round, preserving assets."""
    state = record.get('native_planning_state')
    if (state and record.get('status') == 'storyboard_review' and not image_reports
            and not state.get('error') and state.get('candidate')
            and 'story_review' in state and 'review' in state
            and not any(row.get('problems') for row in state['review'])):
        # The confirmed webpage "replan" action must not silently return the
        # same completed candidate. Automatic continuation never calls here
        # for an already approved plan. Failed/unfinished plans still resume.
        record.setdefault('native_planning_history', []).append(copy.deepcopy(state))
        record['native_planning_history'] = record['native_planning_history'][-5:]
        record.pop('native_planning_state')
        return
    if state:
        from .video_review_policy import strict_story_review
        from .video_dialogue_story_review import partition_problems
        if strict_story_review(record):
            state['strict_story_review'] = True
        elif 'strict_story_review' in state:
            state['strict_story_review'] = False
        blocking, advisory = partition_problems(state.get('story_review'), strict=state.get('strict_story_review', False))
        if advisory or 'story_review_advisories' in record:
            record['story_review_advisories'] = advisory
        if (not image_reports and advisory and not blocking and state.get('candidate')
                and 'review' in state and not any(row.get('problems') for row in state['review'])):
            state.pop('error', None)
            return  # Reuse reviews; no creative retry for authorial suggestions.
    if state and state.get('planning_basis_unverified'):
        # Only this explicit website action may replace a legacy plan whose
        # content provenance cannot be established. Preserve it for inspection.
        record.setdefault('native_planning_history', []).append(copy.deepcopy(state))
        record['native_planning_history'] = record['native_planning_history'][-5:]
        record.pop('native_planning_state')
        return
    if not state or not (state.get('error') or image_reports) or not state.get('candidate'):
        return
    if not image_reports and '按原文顺序完整覆盖' in str(state.get('error', '')):
        from .story_source_alignment import restore_repeated_source
        restored = restore_repeated_source(state['candidate'], record.get('native_story', {}).get('text', ''))
        if restored:
            state.setdefault('source_citation_repairs', []).extend(restored)
            state.pop('error', None)
            record.setdefault('logs', []).append('已按采用稿恢复分镜引用中漏抄的重复字，复用原候选继续校验。')
            return
    if state.get('basis_sha256'):
        from .video_studio import planning_parameters, planning_references
        from .video_dialogue_story_review import REVISION as STORY_REVIEW_REVISION
        parameters, references = planning_parameters(record), planning_references(record)
        basis = dict(story=record['native_story'],speech_mode=parameters['speech_mode'],
            cast=parameters.get('project_cast'),narrator=record.get('native_narrator'),
            settings=record['settings'],references=references)
        fingerprint = _hash({**basis,'story_review_revision':STORY_REVIEW_REVISION,
                             'native_director_revision':NATIVE_DIRECTOR_REVISION})
        if state['basis_sha256'] == _hash(basis) and state.get('fingerprint') != fingerprint:
            # Let the planner perform version compatibility checks first. The
            # explicit route must not discard the candidate ahead of that gate.
            return
    record.setdefault('native_planning_history', []).append(copy.deepcopy(state))
    record['native_planning_history'] = record['native_planning_history'][-5:]
    candidate = state.get('candidate')
    if not isinstance(candidate, dict) or not isinstance(candidate.get('shots'), list):
        previous = (state.get('repair') or {}).get('previous')
        if isinstance(previous, dict) and isinstance(previous.get('shots'), list):
            recovered = copy.deepcopy(state)
            recovered['candidate'] = copy.deepcopy(previous)
            for key in ('review', 'story_review'):
                recovered.pop(key, None)
            if _resume_structurally_valid_candidate(record, recovered):
                state.clear()
                state.update(recovered)
                return
    if not image_reports and _resume_structurally_valid_candidate(record, state):
        return
    from .video_renderability import REVISION as STAGING_REVISION
    if state.get('review') and state.get('staging_review_revision') != STAGING_REVISION:
        # A changed reviewer should first reconsider the saved candidate. Do not
        # spend a fresh director request or destroy the evidence before review.
        _remember_staging_review(state)
        state.pop('review', None)
        state.pop('error', None)
        state['attempt'] = 0
        return
    _remember_staging_review(state)
    state.update(repair=_repair_request(state, image_reports), attempt=0)
    state.pop('version_revalidation', None)
    for key in ('candidate', 'review', 'story_review', 'error', 'validation_shot_ids'):
        state.pop(key, None)


def _localize_saved_prop_repair(repair):
    """Recover old empty repair scopes from typed validation, never error text."""
    if not repair or repair.get('shot_ids'):
        return repair
    previous = repair.get('previous')
    if not isinstance(previous, dict) or not isinstance(previous.get('shots'), list):
        return repair
    from .video_prop_states import compile_timeline, LocalPropBasisError
    try:
        compile_timeline(previous, previous.get('props', []), previous['shots'])
    except LocalPropBasisError as exc:
        return {**copy.deepcopy(repair), 'shot_ids': exc.shot_ids, 'problems': str(exc)}
    except ValueError:
        pass  # Unknown defects must not be assigned a guessed local scope.
    return repair


def _director_envelope(result, field):
    """Unwrap only explicit, unambiguous transport containers."""
    candidate = result
    for _ in range(4):
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                break
        elif isinstance(candidate, dict) and field in candidate:
            value = candidate[field]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    break
            if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
                return {**candidate, field: value}
            break
        elif isinstance(candidate, dict):
            wrappers = [key for key in ('data', 'result', 'output') if key in candidate]
            # Do not discard a prop catalog or merge conflicting story objects.
            if len(wrappers) != 1 or any(key in candidate for key in ('props','prop_state_tracks','shots','replacements')):
                break
            candidate = candidate[wrappers[0]]
        else:
            break
    keys = ', '.join(str(k)[:60] for k in list(result)[:12]) if isinstance(result, dict) else type(result).__name__
    raise ValueError(f'导演返回格式未符合要求：需要非空 {field} 列表，收到字段：{keys or "空对象"}。已保存回复与原草稿，可继续未完成规划，无需新建项目')


def _generate_candidate(ask, system, basis, repair):
    repair = _localize_saved_prop_repair(repair)
    if not repair or not repair.get('shot_ids'):
        result = ask(system, {**basis, 'repair':repair})
        return _director_envelope(result, 'shots')
    previous = repair['previous']['shots']
    targets = repair['shot_ids']
    instructions = '''\n本次仅修订repair.shot_ids指定的镜头，其他镜头由程序原样保留。
repair.story_problems和repair.staging_problems如有提供，是本轮完整问题清单；problems只是界面摘要。联合处理故事与动作问题，不能只修摘要开头的一项。
repair.image_problems是已生成图片的版本绑定观察，提供实际偏差及相邻镜头证据，不是新的故事设定。先区分原设计冲突/不明确与图片单纯未遵守设计；只修前者。原设计正确的镜头原样返回，不为迎合错误图片改写故事或改变正确要求，像素错误留给后续局部修图。correction是建议而非已批准事实，须核对完整原稿、机位和时序后使用。旧图观察不能证明修订后的图片已经合格。
返回JSON {replacements:[{id:"shot_001",shots:[修订后的完整镜头对象]}]}，每个指定id恰好一次。
每个替换的source_text连接必须与该原镜头source_text逐字一致，可拆成连续动作镜头；不可移动或删改其他镜头原文。
原稿中角色的直接引语须逐字保留并绑定原说话人，不借修订添加新的剧情。
每个替换镜头完整返回prop_timeline；局部拆镜也须从原开场状态连续推进到原结束状态，不偷偷改变未修订相邻镜头的状态。
若冲突来自共享道具状态定义，可另返回顶层prop_state_tracks，按原prop_id仅返回需要修正的轨迹，未返回的道具由程序保留；也兼容完整列表。每条返回轨迹保留完整states列表，仅可修正state_repair_scope列出的状态定义，也可增加替换镜头确实使用的新状态。禁止重命名或猜测道具ID。
其他已有状态、initial_state及道具身份全部保留。允许修正的状态也须保持原稿事实；状态是某一时刻的静态快照，不能包含“随后走向”等动作过程或未来状态。
持有者、接触关系、开合或剧情关键位置不同需不同状态；普通手持物随人换地点，不强行增加地理状态。必要移动过程写在action/performance_beats及change_basis，不能把一段移动同时塞进开场和结束定义。
change_basis只说明开场到结束的净状态变化；开场与结束ID相同时留空，镜内暂时变化后复原的动作仍完整保留在action/performance_beats，不能删动作来满足空字段。两个ID不同时引用原稿或action的单个连续片段，不拼接或改写。
未涉及状态定义的修订不用返回prop_state_tracks。修订后全片仍会重新验证状态连续性与语义，不可用改状态定义掩盖缺少物理衔接。
source_text只是追溯原稿的完整依据，不要求将其中每个字都读出。对审核指出的重复解说，可删去或缩短导演新编的画外旁白；原稿事实仍须通过动作、音效或必要台词完整传达。
对empty_repetition修订，不要为了“保留台词”又把已要求删去的重复旁白加回来；角色直接引语不受此例外影响。
起始画面和中文action及英文scene需描述同一个位置和动作；已位于窗边等位置时，不再写走到同一位置。
允许纯动作镜头turns=[]。自然的短距离走动和简短问候可安排在同镜，但要留足动作及说话时间。
指定镜头可能包含审核明确引用的前后状态依赖。先核对相邻镜头的结束与开场；仅在确有衔接冲突时修改这些依赖，否则原样返回。
修订前核对未指定镜头的既有空间、布景、人物关系和通用文字限制。优先用现有动作、道具或取景表达原稿事实，不为解决单镜表达新增会改变其余镜头布景的装饰。若持续布景确需调整，只能在已列明依赖范围内统一；不得假定未返回的其他镜头会自动继承本镜新增内容，也不能靠删掉原稿事实回避冲突。
原稿既定的后续动作需要短过渡时，可在相关镜头内补足必要的物理衔接，不通过凭空切换状态来省略必要动作，也不为过渡另编台词。若故事审稿指出原文事实尚未表达，可将该原文叙述转成简短自然对白；不得新增人物、关键事件或原文没有的事实，已有直接引语仍逐字保留。
'''
    from .video_prop_states import repair_scope, merge_repaired_tracks
    result = ask(system + instructions, {**basis, 'repair':repair,
        'state_repair_scope': repair_scope(repair['previous'], targets)})
    # Empty replacements retain the existing precise scope error below.
    if not isinstance(result, dict) or 'replacements' not in result or isinstance(result['replacements'],str):
        result = _director_envelope(result, 'replacements')
    rows = result.get('replacements') if isinstance(result, dict) else None
    if (not isinstance(rows, list) or any(not isinstance(r, dict) or not isinstance(r.get('id'), str) for r in rows)
            or sorted(r['id'] for r in rows) != targets):
        raise ValueError('局部分镜修订未完整对应失败镜头，原草稿已保留')
    replacements = {r['id']:r.get('shots') for r in rows}
    merged = []
    compact = lambda text: ''.join(text.split())
    for index, old in enumerate(previous, 1):
        identity = f'shot_{index:03d}'
        if identity not in replacements:
            merged.append(copy.deepcopy(old));continue
        new = replacements[identity]
        if (not isinstance(new, list) or not new or any(not isinstance(row, dict) or not isinstance(row.get('source_text'),str) for row in new)
                or compact(''.join(row['source_text'] for row in new)) != compact(old['source_text'])):
            raise ValueError('局部分镜修订删改了原文，未覆盖原草稿')
        merged.extend(new)
    candidate = {**copy.deepcopy(repair['previous']), 'shots':merged}
    if 'prop_state_tracks' in result:
        candidate = merge_repaired_tracks(repair['previous'], candidate, result['prop_state_tracks'], targets)
    return candidate


def plan_native_story(record, references, parameters, *, progress, persist, ask=None):
    from .video_agents import ask_json, audit_storyboard
    from .video_renderability import review_designs, STORY_STAGING, REVISION as STAGING_REVISION
    from .video_dialogue_story_review import review_story, REVISION as STORY_REVIEW_REVISION
    # Long, bilingual storyboards can exceed a relay's non-streaming read window.
    # Only this planning route opts in; injected agents and legacy calls retain
    # their existing protocol. The transport accepts only a fully ended answer.
    # A bilingual full-film plan includes dialogue, image composition and
    # continuous prop/performance states. Give generation and its bounded
    # repair enough output space; review-only calls retain the smaller budget.
    ask = ask or (lambda system, data: ask_json(system, data, stream=True,
        max_output_tokens=10000 if 'performed_shots' in data or 'shots' in data else 24000))
    cast = parameters.get('project_cast')
    if not cast:
        raise ValueError('对话故事需要先完成本项目角色设计与绑定')
    settings = record['settings']
    from .story_genre_profiles import production_profile, supplement
    genre = production_profile(parameters)
    director_system = NATIVE_DIRECTOR + STORY_STAGING + supplement(genre, 'director')
    basis = dict(story=record['native_story'], speech_mode=parameters['speech_mode'],
        cast=cast, narrator=record.get('native_narrator'), settings=settings, references=references)
    fingerprint = _hash({**basis, 'story_review_revision': STORY_REVIEW_REVISION,
                         'native_director_revision': NATIVE_DIRECTOR_REVISION})
    basis_sha256 = _hash(basis)
    state = record.get('native_planning_state')
    if state and state.get('fingerprint') != fingerprint:
        saved_basis = state.get('basis_sha256')
        known_legacy = _hash({**basis, 'story_review_revision': STORY_REVIEW_REVISION,
            'native_director_revision':'native-performance-timeline-v9-held-prop-relations'})
        if saved_basis == basis_sha256 or (not saved_basis and state.get('fingerprint') == known_legacy):
            # A code update is not a request for a different story. Compile
            # and review the existing candidate before any creative request.
            pending_repair = state.get('repair') or {}
            resumable_repair = (bool(pending_repair.get('shot_ids'))
                                and isinstance(pending_repair.get('previous'), dict)
                                and bool(pending_repair['previous'].get('shots')))
            if 'candidate' not in state and not resumable_repair:
                state['error'] = '规划版本已变化且缺少可复核候选，已保留原记录；请显式重新规划'
                state['planning_basis_unverified'] = True
                persist()
                raise ValueError(state['error'])
            _remember_staging_review(state)
            state.pop('review', None)
            state.pop('story_review', None)
            state.update(fingerprint=fingerprint)
            if 'candidate' in state:
                state['version_revalidation'] = True
            else:
                # A failed local response has not replaced the saved draft.
                # Resume the same repair scope on an explicit user retry.
                state.pop('version_revalidation', None)
        elif saved_basis:
            record.setdefault('native_planning_history', []).append(copy.deepcopy(state))
            record['native_planning_history'] = record['native_planning_history'][-5:]
            state = None  # The adopted content really changed.
        else:
            state['error'] = '旧规划缺少可核实的内容来源，未丢弃候选或重新调用模型；请显式重新规划'
            state['planning_basis_unverified'] = True
            persist()
            raise ValueError(state['error'])
    if not state:
        state = {'fingerprint':fingerprint, 'attempt':0}
        record['native_planning_state'] = state
    state.update(basis_sha256=basis_sha256, native_director_revision=NATIVE_DIRECTOR_REVISION,
                 story_review_revision=STORY_REVIEW_REVISION)
    from .video_review_policy import strict_story_review
    state['strict_story_review'] = strict_story_review(record)
    persist()
    if state.get('staging_review_revision') != STAGING_REVISION:
        if state.get('staging_review_revision') and 'candidate' in state:
            state['version_revalidation'] = True
        _remember_staging_review(state)
        state.pop('review', None)
        state['staging_review_revision'] = STAGING_REVISION
        persist()
    while True:
        if 'candidate' not in state:
            repair = _localize_saved_prop_repair(state.get('repair'))
            if repair is not None:
                state['repair'] = repair
            request_key = _hash({'basis': basis, 'repair': repair})
            cached = state.get('pending_director_response') or {}
            recovered = None
            previous_error = ''
            if cached.get('request_key') == request_key and 'response' in cached:
                try:
                    recovered = _generate_candidate(lambda *_: copy.deepcopy(cached['response']),
                        director_system, basis, repair)
                except ValueError as exc:
                    previous_error = str(exc)
            if recovered is not None:
                progress('已复用保存的规划回复完成本地校验，不重复调用导演模型；继续后续审核。')
                state['candidate'] = recovered
            else:
                targets = (repair or {}).get('shot_ids')
                progress(('只修订未通过的镜头：' + '、'.join(targets) + '；其他镜头原样保留。')
                         if targets else '按本故事角色规划 H3 原生对白、音色归属和镜头动作。')
                def capture_response(system, payload):
                    from .video_timing_policy import story_speech_budget
                    payload = {**payload, 'source_speech_budget':
                               story_speech_budget(record['native_story']['text']),
                               'dialogue_route': parameters.get('dialogue_route', 'h3_native'),
                               'dialogue_tts_engine': parameters.get('dialogue_tts_engine', 'indextts25')}
                    if previous_error:
                        payload = {**payload, 'previous_invalid_response': copy.deepcopy(cached['response']),
                                   'validation_error': previous_error}
                        system += '\n上次返回因validation_error未能合并。仅修正该错误，保持既定修订范围和已完成内容，不从头重写。'
                    response = ask(system, payload)
                    state['pending_director_response'] = dict(request_key=request_key, response=copy.deepcopy(response))
                    persist()  # Save a paid reply before local merging can fail.
                    return response
                try:
                    state['candidate'] = _generate_candidate(capture_response,
                        director_system, basis, repair)
                except ValueError as exc:
                    state['error'] = str(exc)
                    persist()
                    raise
            state.pop('pending_director_response', None)
            state.pop('error', None)
            persist()
        try:
            from .story_source_alignment import restore_repeated_source
            citation_repairs = restore_repeated_source(state['candidate'], record['native_story']['text'])
            if citation_repairs:
                state.setdefault('source_citation_repairs', []).extend(citation_repairs)
                progress('已按采用稿恢复分镜原文引用中漏抄的重复字；台词与动作不变，继续完整覆盖校验。')
                persist()
            if isinstance(state['candidate'], dict) and state['candidate'].get('props') and 'prop_state_tracks' not in state['candidate']:
                raise ValueError('新规划缺少关键道具的跨镜状态轨迹，尚未生成图片')
            scenes, shots = compile_storyboard(state['candidate'], record, cast, references)
            if any(not shot['native_dialogue'].get('performance_beats') for shot in shots):
                raise ValueError('新分镜缺少动作与台词统一的表演节拍，请补齐每句台词及安静间隙，再生成图片')
            context = dict(project_cast=copy.deepcopy(cast), production_text=record['native_story']['text'],
                user_global_character_bible=settings['characters'], world_bible=settings['world'],
                video_direction=dict(dynamic_text_mode='visual_first', visual_style=settings['style']),
                native_speech_mode=parameters['speech_mode'], story_genre_profile=genre)
            context['production_fingerprint'] = planning_fingerprint(scenes, settings['style'],
                settings['characters'], settings['world'], references, parameters)
            if 'story_review' not in state:
                progress('独立审核实际对白与动作能否讲清故事，检查说话人、动机和结局。')
                state['story_review'] = review_story(context, shots, ask=ask)
                persist()
            # Complete both independent reviews of this structurally valid
            # candidate before spending its single repair. A story-only repair
            # must not hide first-pass staging defects until the budget is gone.
            if 'review' not in state:
                progress('独立检查对话分镜的身份、动作密度和开场衔接。')
                state['review'] = review_designs(_staging_context(context, state), shots, ask=ask)
                persist()
            failures = [row for row in state['review'] if row['problems']]
            problems = []
            from .video_dialogue_story_review import partition_problems
            blocking_story, advisories = partition_problems(state['story_review'], strict=state['strict_story_review'])
            record['story_review_advisories'] = advisories
            if blocking_story:
                problems.append('对白故事表达未通过：' + json.dumps(blocking_story, ensure_ascii=False)[:3000])
            if failures:
                problems.append('对话镜头设计未通过：' + json.dumps(failures, ensure_ascii=False)[:2000])
            if problems:
                raise ValueError('；'.join(problems))
            approved_shots = audit_storyboard(shots, {ref['id'] for ref in references})
            state.pop('error', None)
            state.pop('validation_shot_ids', None)
            state.pop('version_revalidation', None)
            persist()
            return context, scenes, approved_shots
        except ValueError as exc:
            from .video_prop_states import LocalPropBasisError
            # Only typed, localized errors may narrow repair scope. Do not
            # guess an affected shot from arbitrary model/error prose.
            state['validation_shot_ids'] = exc.shot_ids if isinstance(exc, LocalPropBasisError) else []
            state['error'] = str(exc)
            persist()
            if state.get('version_revalidation'):
                # Version compatibility failure needs a deliberate decision;
                # never turn an upgrade into a paid automatic rewrite.
                raise
            if state['attempt'] >= 1 or (state.get('repair') or {}).get('image_problems'):
                # A deliberate repair of produced-image findings gets one
                # candidate. Preserve its evidence on rejection; do not spend
                # another automatic rewrite or lose the image feedback.
                raise
            _remember_staging_review(state)
            state.update(attempt=1, repair=_repair_request(state))
            state.pop('candidate')
            state.pop('review', None)
            state.pop('story_review', None)
            persist()
            progress('对话设计需要修订，自动修订并复核一次；仍失败则保留草稿暂停。')
