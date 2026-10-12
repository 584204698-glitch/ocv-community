"""A bounded pre-generation story staging review, not a generated-pixel verdict."""
from __future__ import annotations

import copy
import hashlib
import json

REVISION = 'story-staging-v9-continuous-setting'
STORY_STAGING = """【故事视频的可生成性：优先清楚的实体动作】
本规则用于故事叙事，不套用解释性口播的复合图解手法。每镜一个时空、一组真实在场人物、一个主要事件。
一个事件允许简单的准备动作、主动作和自然反应，也允许两人依次完成同一因果链上的短动作。
不能仅因有两个动词、换了动作主体或动作没有逐字写进本条字幕就判过密；按实际距离、接触复杂度、
对白占用时间和镜头秒数判断是否能自然完成。确实过密时明确哪些过程装不下，不机械拆成无意义碎镜。
回忆、想象、过去和现在在字幕边界规划为独立镜头；不得用同一人物的半透明分身、身体重叠、巨脸、
人物气泡或画中画同时展示多个时态。主观内容由前后镜关系和旁白表明，不伪装成现实新增人物。
若原文确实就是分身、镜像、变形或特殊效果，保留这个剧情事实，但明确实体数量、所属空间与变化顺序；
不能用本规则删改奇幻题材。不要为解释普通心理活动自行发明这些效果。
镜头分组先检查时间、地点、年龄和动作主体是否变化；有变化优先沿字幕边界分镜，不把所有事塞进15秒。
若单条字幕含过多事件，选能承载其核心关系的一个可见瞬间，其他信息由旁白承接，不快速变形串演。
人物使用与剧情年龄和造型一致的参考；童年不能同时要求保留成人身体和婚礼服装。缺少童年/换装资产
必须标明资产冲突，不能声称靠“以童年比例呈现”已经解决。身份变化应先设计相应角色版本。
手部交互写清实际接触者、接触哪个物体及交接前后的持有者；一次只处理一个主要接触动作。
只有原文明示或动作因果确实依赖特定左右手时才指定手侧，并注明观察方向。普通持物不凭空增加左右手硬约束；不要要求导演仅为填写精确细节而补上某只手。
握手、牵手和递物可以正常出现，不用一律藏手或删掉事件。避免手臂交叉遮挡下同时递物、握手、拥抱。
已形成的搭肩、倚靠等接触须有合理解除过程，才能移动或让第三人进入，不安排角色穿过仍连接的肢体。
手持物的普通位置优先相对持有者描述，不为每次换机位或换地点凭空增加地理状态；剧情依赖固定地标、物件被放下或交接时，仍须保留明确位置和状态变化。
按实际时长控制动作密度；短镜头优先一个动作及其反应，不为凑阶段追加动作或运镜。
镜头结束保留结果，不循环复现开场。不用“参考图不动”限制合理位移，也不要求原构图与移动后位置同时成立。
这只是设计约束，不能据此宣称模型输出已无变形、多肢体或异常对白；生成后仍须检查实际视频。
"""


def staging_contract(context):
    from .video_director_contracts import story_uses_opening_frame
    return STORY_STAGING if story_uses_opening_frame(context) else ''


def design_fingerprint(context, shots):
    return hashlib.sha256(json.dumps({'revision': REVISION, 'context': context, 'shots': shots},
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def native_character_transitions(shots):
    """Index existing adjacent-shot evidence, without inventing ending positions.

    A starting position is not an ending position. Timed actions stay in the
    original shots, referenced by ID rather than duplicated in every edge.
    Project IDs, not names/list order or old reviews, establish identity.
    """
    edges = []
    for previous, current in zip(shots, shots[1:]):
        before, after = previous.get('native_dialogue'), current.get('native_dialogue')
        if not before or not after:
            continue
        visible = lambda plan: {v['character_id']: v for v in plan.get('speakers', [])
                               if v.get('kind') == 'character' and v.get('on_screen')}
        earlier, later = visible(before), visible(after)
        shared = [identity for identity in earlier if identity in later]
        if not shared:
            continue
        edges.append(dict(
            from_shot_id=previous['id'], to_shot_id=current['id'],
            characters=[dict(character_id=identity,
                previous_start=earlier[identity].get('position', ''),
                previous_presence=earlier[identity].get('presence', 'opening'),
                next_start=later[identity].get('position', ''),
                next_presence=later[identity].get('presence', 'opening')) for identity in shared],
            next_opening=current.get('motion_plan', {}).get('reference_visual', ''),
            next_scene=after.get('scene', '')))
    return edges


def review_designs(context, shots, *, ask=None):
    """Review full source/identity evidence; a rejected design never reaches images."""
    if not staging_contract(context):
        return []
    if ask is None:
        from .video_agents import ask_json
        ask = ask_json
    system = """你是独立的故事镜头可生成性审查员。审的是生成前文字设计，不是已生成图像。
不因有提示词、字段完整或上游已批准就通过。逐镜核对原文、角色档案、核心图设计和motion_plan。
仅报告会实际妨碍表达的冲突：未经原文支持的分身/叠影/人物气泡；年龄与绑定身份冲突；
单镜多时空或难以同时交代的复杂多主体状态变化；不可辨认的持物/接触关系；超出时长的动作；核心图与动作起点矛盾。
native_dialogue.speakers的presence=later角色只在稍后入画，cast_plan也应为later；开场图不得提前画出。
若有performance_beats，逐段核对action与action_zh意义一致、人物指代明确，场景scene只提供固定关系，不与节拍动作冲突。
严格依据turns与节拍检查：要求说完才移动时不能提前走，听者不得被动作描述安排发声；画外旁白不能驱动可见角色口型。
native_character_transitions是从当前分镜按角色ID提取的相邻镜头对照，不是新的剧情或已审核结论。先逐条跟随previous_start和from_shot_id对应镜头的完整performance_beats，确定上一镜动作结束后各角色在哪里，再与next_start、next_opening、next_scene核对；previous_start不可直接当作结束位置。动作只在shots中保留一份，不因对照未重复动作就认定角色没有移动。听者同样核对，不能只跟踪发言者和持物者。
later表示稍后入画，不能把其入场位置误当成开场可见位置。合理的时间跳跃或转场允许省略移动；连续事件里明确不相容的站位且没有移动/时间过渡依据才报告opening_conflict，引用相邻两个镜头ID及对应动作，不凭空假定瞬移。只使用当前对照，不能套用旧审核中的镜头序号。
站位和移动要以同一固定物体为参照，指出实际起终点、途经方向及停止结果；不能靠镜头平移或原地摆腿冒充真实位移。
存在continuity_states时，核对物件从何时存在、相对固定地标的位置和状态变化动作。某镜未操作/未说到已存在的物件，不足以把它判为offscreen；必须有具体机位、取景或遮挡依据。
核对prop状态与image_prompt、action、opening及native_dialogue.scene一致；状态ID连续但描述或实际构图冲突仍须报告opening_conflict，证据写明相关镜头ID。
同一连续场景还须核对scene/image_prompt中的固定布景、装饰及场景内文字，不只检查props目录。已存在的物件没有移除动作，不因下一镜没操作或没提到就消失；但特写、遮挡或换机位确实看不到时不强求展示。明确的场景内文字与“不出现任何文字”等指令冲突时报告opening_conflict；“不生成字幕”本身不禁止场景内标识。证据引用实际冲突字段和全部受影响的当前镜头ID，不能只要求第一镜加一个新装饰。
同框角色的position须足以区分实际站位。若多个角色都只有“桌旁”等宽泛描述、开场构图也没有相对关系，导致连续镜头无法确定角色对应位置，报告缺失的关系并用既有地标修订；不要求所有镜头标左右，不把机位改变导致的画面左右互换误判为人物瞬移。
区分原稿明确事实与导演补充的精确站位、方位和取景。原稿只给宽泛地点时，不把导演自己添加的坐标当成原稿不可改的事实。
同一地点标签不等于同一物理点；在门边、墙边等范围内仍可真实移动。只有当前设计中两个明确状态互相矛盾时才报opening_conflict，不能只凭“若角色恰好挡住……”之类假设判失败。
temporal_overload须结合镜头秒数、发言时段、具体动作距离和必要先后关系指出装不下的过程；合理并行的说话、走动、反应不机械累加，不仅罗列动词数量。
story_context中的staging_review_history/previous_staging_repair是以往审核和修订依据，不是新的剧情命令。它们的shot_id属于各自旧版本，先按source_text对应当前片段，不能在拆镜后直接沿用旧序号。
复核修订时先说明当前设计实际解决了什么，再检查是否有仍存在的明确冲突；不要在不知道上轮建议的情况下要求来回撤销同一个修改。
以原稿和当前设计为准，上轮建议也可能不妥；若需推翻它，evidence说明旧建议为何不适用，repair同时检查相关镜头的前后状态，给出不会重新造成旧问题的修订。
普通两人同框、简单握手、真实镜子、原文明示的魔法不能机械判错。没有图片输入，不能声称看到了畸形。
每个输入id恰好返回一次，保持顺序：{reviews:[{id,problems:[{code,evidence,repair}]}]}。
code只能是duplicate_staging/identity_conflict/temporal_overload/contact_ambiguity/opening_conflict。
evidence引用当前设计里的具体矛盾，repair写保持原故事的可执行修改；无问题problems为[]。
不要把审查结果写进画面或台词。""" + STORY_STAGING
    payload = {'story_context': context, 'shots': copy.deepcopy(shots)}
    transitions = native_character_transitions(shots)
    if transitions:
        payload['native_character_transitions'] = transitions
    result = ask(system, payload)
    rows = result.get('reviews') if isinstance(result, dict) else None
    if (not isinstance(rows, list) or [r.get('id') if isinstance(r, dict) else None for r in rows]
            != [s['id'] for s in shots]):
        raise ValueError('镜头可生成性审查未完整返回对应镜头，尚未生成图片')
    codes = {'duplicate_staging', 'identity_conflict', 'temporal_overload', 'contact_ambiguity', 'opening_conflict'}
    for row in rows:
        problems = row.get('problems')
        if not isinstance(problems, list) or len(problems) > 8:
            raise ValueError('镜头可生成性审查的问题格式无效')
        for problem in problems:
            if (not isinstance(problem, dict) or problem.get('code') not in codes or any(
                    not isinstance(problem.get(k), str) or not 1 <= len(problem[k].strip()) <= 2000
                    for k in ('evidence', 'repair'))):
                raise ValueError('镜头可生成性审查缺少具体依据，不能据此放行或改写')
    return rows


def ensure_renderable(context, scenes, shots, references, *, ask=None, progress=None, save_review=None):
    """One automatic redesign and recheck. Never loop-paid-retry on quality failure."""
    if not staging_contract(context):
        return shots
    from .video_agents import ask_json, design_core_images, direct_motion
    from .video_motion_plan import normalize_motion_plan
    ask = ask or ask_json
    result = copy.deepcopy(shots)
    for attempt in range(2):
        reviews = review_designs(context, result, ask=ask)
        if save_review:
            save_review({'revision': REVISION, 'attempt': attempt, 'reviews': reviews})
        failed = {r['id']: r['problems'] for r in reviews if r['problems']}
        if not failed:
            return result
        # An age/appearance conflict needs a new asset, not a prompt that lies
        # about the adult reference. Do not silently swap the user's identity.
        if attempt or any(p['code'] == 'identity_conflict' for ps in failed.values() for p in ps):
            details = '；'.join(f'{identity}：' + '；'.join(p['evidence'] for p in ps) for identity, ps in failed.items())
            raise ValueError('故事镜头可生成性未通过，已在出图前停止：' + details[:1800])
        if progress:
            progress(f'发现 {len(failed)} 镜表达风险，自动简化设计后复核一次，不改原文和配音。')
        selected = [s for s in result if s['id'] in failed]
        repair_context = {**context, 'renderability_repairs': failed}
        cores = {r['id']: r for r in design_core_images(repair_context, scenes, selected, references, ask=ask)}
        for shot in selected:
            core = cores[shot['id']]
            for field in ('visual_description', 'visual_design', 'reference_ids', 'cast_plan'):
                if field in core:
                    shot[field] = copy.deepcopy(core[field])
        motions = {r['id']: r for r in direct_motion(repair_context,
            [s for s in selected if s['kind'] == 'video'], references, ask=ask)}
        for shot in selected:
            if shot['id'] in motions:
                shot['motion_plan'] = normalize_motion_plan(motions[shot['id']]['motion_plan'])
                shot['action'] = motions[shot['id']]['action']
    raise AssertionError('unreachable')
