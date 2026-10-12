# SPDX-License-Identifier: AGPL-3.0-only
"""Pure contracts for one narration draft; no provider, storage or media calls.

The A release generates one candidate. Structural validation is not editorial
approval. Character IDs are suggestions scoped to this draft, never asset IDs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


STORY_PRESET_VERSION = "2026-09-30.v3"
STORY_PRESETS: dict[str, dict[str, str]] = {
    "general": {
        "id": "general", "label": "自由短故事",
        "description": "围绕你的想法发展一条清楚、适合朗读的故事线。",
        "instruction": "不预设儿童受众、动物主角或画风；从用户给出的事件、人物或结尾发展故事，允许安静而有变化的小事。",
    },
    "warm_relationship": {
        "id": "warm_relationship", "label": "温暖关系",
        "description": "用具体选择和行动表现人物之间的关系变化。",
        "instruction": "让温暖来自有动机、有代价的小选择和可见反应；不强制牺牲、疾病、哭泣或道德总结。",
    },
    "micro_mystery": {
        "id": "micro_mystery", "label": "日常微悬念",
        "description": "从一个生活中的异常出发，让已有线索带来新的解释。",
        "instruction": "开场提出一个具体疑问，线索逐步改变观众的理解；结尾用此前出现的信息兑现，不突然新增秘密解决一切。",
    },
    "fantasy_rule": {
        "id": "fantasy_rule", "label": "奇幻规则",
        "description": "一条容易理解的世界规则，带来选择与后果。",
        "instruction": "确立一条清楚的奇幻规则及其限制，让人物选择造成后果；结尾遵守已建立的规则，不临时新增能力。",
    },
    "miniature_world": {
        "id": "miniature_world", "label": "微缩世界与物件",
        "description": "从尺度、用途或物件视角发现一件值得讲述的小事。",
        "instruction": "让尺度或物件用途参与因果，保持空间与物理关系可理解；不能只有漂亮设定，必须有行动、发现或状态变化。",
    },
    "picturebook": {
        "id": "picturebook", "label": "儿童绘本",
        "description": "明确选择儿童受众的温和故事，画风另行决定。",
        "instruction": "以儿童能理解的愿望、行动、结果与感受组织故事；用户未指定年龄时面向4—8岁，避免恐怖与说教，不固定动物主角。",
    },
    "everyday_comedy": {
        "id": "everyday_comedy", "label": "搞笑日常",
        "description": "一个好懂的小麻烦，越解决越有趣，最后用人物反应收住笑点。",
        "instruction": "用具体处境、合理但事与愿违的尝试和可见反应形成喜剧；每次尝试带来新变化，不能重复同一个笑话。人物可以认真、笨拙或固执，但不能为笑点突然失去常识。不依赖网络热梗、羞辱弱者、解释哪里好笑或末尾上价值。",
    },
    "speculative_life": {
        "id": "speculative_life", "label": "科幻生活",
        "description": "假如生活多了一种新技术，它会带来什么意想不到的小麻烦？",
        "instruction": "只引入一项容易理解的新技术或未来生活条件，尽早通过一次具体使用呈现它的用途和限制。让人物的实际选择产生后果，不把正文写成设定说明书；结尾不能临时添加技术能力解围，也不固定为机器人觉醒、科技害人或抽象人性说教。",
    },
    "knowledge_story": {
        "id": "knowledge_story", "label": "知识小故事",
        "description": "从一个生活疑问或小实验出发，在事件中弄明白一个知识点。",
        "instruction": "围绕一个可靠、基础、可解释的知识点安排观察、尝试与结果。开头先有具体疑问，让观众看见答案如何出现；人物的错误猜想要被事件澄清。不能把拟人寓言、虚构技术或巧合当科学原理；无法确定的事实不编造数据、实验结论或出处，应换用已知基础现象。不要大段上课或堆多个知识点。",
    },
}

_INTENTS = {"idea", "topics", "manuscript"}
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")


def _text(value: Any, code: str, *, minimum: int = 1, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(code)
    result = value.strip()
    if not minimum <= len(result) <= maximum or _CONTROL.search(result):
        raise ValueError(code)
    return result


def canonical_story_brief(
    idea: str = "", preset: str = "general", duration_seconds: int = 60,
    intent: str = "idea",
) -> dict[str, Any]:
    """Validate a writing brief; manuscript text is validated by its caller.

    An idea must be supplied for idea mode. Topics mode may invent one topic;
    it does not promise a multi-candidate selection or use unseen history.
    """
    if not isinstance(preset, str) or preset not in STORY_PRESETS:
        raise ValueError("STORY_PRESET_INVALID")
    if not isinstance(intent, str) or intent not in _INTENTS:
        raise ValueError("STORY_INTENT_INVALID")
    if type(duration_seconds) is not int or not 15 <= duration_seconds <= 600:
        raise ValueError("STORY_DURATION_INVALID")
    normalized_idea = _text(idea, "STORY_IDEA_INVALID", minimum=1 if intent == "idea" else 0, maximum=4000)
    return {
        "idea": normalized_idea,
        "preset": preset,
        "preset_version": STORY_PRESET_VERSION,
        "duration_seconds": duration_seconds,
        "intent": intent,
        "narrative_mode": "narration",
        "generation_stage": "manuscript_import" if intent == "manuscript" else "single_draft",
        "duration_estimate": {
            "min_characters": round(duration_seconds * 2.2),
            "max_characters": round(duration_seconds * 3.3),
            "calibrated": False,
            "note": "按未校准的中文旁白语速粗估，并预留动作与停顿；字数只作写作参考，不保证成片时长，实际以配音和字幕时间轴为准。",
        },
    }


STORY_WRITER_SYSTEM_PROMPT = """你是 OCV 的中文短故事写作者。当前阶段只生成一份待审阅的旁白故事草稿；没有执行三方向筛选、独立编辑审稿或视频制作，不得声称已经通过这些阶段。

用户明确要求优先于预设偏好。保留用户指定的人物、关系、结尾、世界规则和已确认造型；不为了“优化”偷偷替换。预设只约束叙事方向，题材、受众、声音形式与画风分别理解，水彩不等于儿童故事。除了明确选择儿童绘本或用户要求儿童受众，不默认写儿童绘本、动物故事或寓言。信息不足时只在未限定的部分合理创作，不声称读过未提供的近期作品或角色库。

创作要求：
- 开头尽快发生一件具体的事，或提出一个值得了解的问题；不要套用固定营销开场。
- 下笔前明确这篇故事让目标观众想跟下去的具体理由：想看一次尝试能否成功、下一次会闹什么笑话、发现怎样的细节，或人物会怎样回应。把这个期待写进事件，不在正文解释“故事很吸引人”。只是有人遇到困难、得到帮助、互相道谢，还不是完整的观看理由。
- 人物为什么行动、行动如何导致结果要清楚。重要解决条件提前铺垫，不能依赖连续巧合或结尾新增能力。
- 别为了让情节成立而绕开常识：人物面对小问题时会先尝试符合性格的简单办法。若必须靠奇怪举动、没来由的调查或大量事后解释才能继续，应换一个事件骨架，而不是给不自然的行为补借口。
- 用可见的动作、物件、状态变化和具体反应承载情绪，少写空泛形容词、重复解释和泛化人生道理。
- 用户提供已有作品片段作参考时，先保留其具体触发、人物选择与对方回应的关系，不把名场面简化为两句标志性台词。只依据实际提供的资料，不声称看过未提供的原片；借鉴已确认的表达方法，不把参考作品的额外人物、事件或回忆擅自移入当前故事。
- 用自然、听一遍能懂的语言，交代清楚代词与时序。宁静故事也可以有细小变化，不强制反派、三幕比例或惊天反转。
- 中间每段至少带来新的尝试、阻力、发现或反应，不能用一串相似小事拖到总结。结尾兑现这篇故事建立的期待，可以只是一个准确的小动作；不能靠“他终于明白了”“原来真正的……”替代已经发生的结果。
- 结尾回应开场和人物选择，允许克制留白；不用说教总结替代故事结果。
- 以少量主要人物、集中场景和可理解的动作链为起点，必要时保留合理复杂性；不把全部故事写成一个人物站着解说。
- 本轮是旁白路线，可用自然叙述转述交谈或包含必要引语，但不依赖多角色配音、口型同步或模型生成对白才能理解故事。
- 目标时长只用于估算篇幅，预留动作与停顿，以实际 TTS 为准，不宣称精确达到秒数。不要为了凑字数篡改用户指定事实。

正文只包含观众应听到的故事。制作说明、镜头、分镜表、运镜、字幕、音效、配乐与时长指令不得混入正文，不写“旁白：”“镜头1：”等制作标签，不输出代码或占位稿。

只输出严格 JSON 对象，不要 Markdown，恰好包含 title、story、synopsis、characters、scenes：
title：建议40字内，最多80字；story：完整可朗读正文，20—12000字，篇幅按本次目标时长估算；synopsis：1000字内的简短梗概。
characters：数组，每项恰好包含 character_id、name、role、description；没有角色时为[]。
scenes：数组，每项恰好包含 scene_id、title、summary；没有明确场景时为[]。
人物与场景摘要只概括最终正文已有信息，不额外编造正文没有的外貌、服装、关系或情节。人物最多16项，场景最多32项。
character_id、scene_id 使用英文小写字母开头的小写字母、数字、下划线，最多64字符，各自在数组中唯一。它们只是本草稿内的身份建议：不凭同名或文件名认定已有资产，不声称已绑定参考图。已明确提供稳定ID时沿用，未提供时可用 character_01、scene_01 等局部ID。
禁止输出质量分数、审核通过、成功状态或其他字段；不得把拒绝、报错或无法完成的说明包装成故事。"""


PICTUREBOOK_WRITER_SYSTEM_PROMPT = """你是中文绘本故事作者，为孩子写一篇愿意听完的完整故事。用户未指定年龄时面向4—8岁。
从孩子能亲近的愿望、好奇、游戏或感受出发，自主构思人物与事件。让人物有自己的想法和反应，让事情在行动、交谈、发现中自然发展；可以温柔、好笑、奇妙，也可以安静。不把人物仅当成完成任务的工具，不靠末尾讲道理赋予前文意义。
给有趣的过程和人物相处留出篇幅；不要只交代问题、解决办法、感谢和总结。重复可以形成节奏，但每次应有新的反应或感受。不强制危险、秘密、反转或固定次数的尝试。拟人和幻想按故事自身规则展开，语言具体、自然，孩子听一遍能理解。
忠实保留用户明确的人物、关系、造型、事件与结局；未限定处才自主创作。不沿用未提供的旧项目角色或资产，不模仿示例故事。若提供 selected_direction，沿这个方向发展；否则直接构思完整故事，不先输出选题报告。
正文是可朗读的故事，允许必要的自然对白和拟声词；旁白路线由讲述者读出，不要求多角色配音或口型。禁止把分镜、运镜、音轨、字幕、时长指令或编辑评价写入正文。时长和字数仅为估计，不能压掉必要的发展，也不靠重复凑字。
""" + STORY_WRITER_SYSTEM_PROMPT[STORY_WRITER_SYSTEM_PROMPT.index("只输出严格 JSON 对象"):]


def story_writer_system_prompt(brief: dict[str, Any]) -> str:
    from .story_genre_profiles import profile_id, supplement
    base = PICTUREBOOK_WRITER_SYSTEM_PROMPT if brief.get("preset") == "picturebook" else STORY_WRITER_SYSTEM_PROMPT
    return base + supplement(profile_id(brief), 'writer')


def story_writer_user_prompt(brief: dict[str, Any]) -> str:
    """Build a prompt from a validated brief, ignoring caller-supplied metadata."""
    if not isinstance(brief, dict):
        raise ValueError("STORY_BRIEF_INVALID")
    normalized = canonical_story_brief(
        idea=brief.get("idea", ""), preset=brief.get("preset", "general"),
        duration_seconds=brief.get("duration_seconds", 60), intent=brief.get("intent", "idea"),
    )
    if normalized["intent"] == "manuscript":
        raise ValueError("STORY_MANUSCRIPT_MODEL_CALL_NOT_ALLOWED")
    return json.dumps({
        "writing_brief": normalized,
        "preset_instruction": STORY_PRESETS[normalized["preset"]]["instruction"],
        "task": ("围绕用户想法写一份完整草稿。" if normalized["intent"] == "idea" else
                 "根据提供的偏好，自主选择一个新选题并写一份草稿；本轮不生成多个方向，不声称已比较频道历史。"),
        "user_constraints": normalized["idea"],
        "output_boundary": "创作要求仅用于写作，不能逐项复述进正文。人物和场景摘要必须对应这次最终正文。",
    }, ensure_ascii=False)


_PRODUCTION_DIRECTION = re.compile(
    r"(?m:(?:^|[。！？!?；;\n])[ \t]*(?:镜头|画面)\s*(?:切换到|切到|转向|拉近|推近|拉远|推进|摇向|移向|对准|聚焦|定格|淡入|淡出))"
    r"|(?m:(?:^|[。！？!?；;\n])[ \t]*(?:在)?(?:镜头|画面)\s*(?:即将)?(?:结束|收尾)(?:之)?(?:前|时|后))"
    r"|(?im:^[ \t]*(?:\[|【)?(?:镜头|分镜)\s*(?:\d+|[一二三四五六七八九十]+)\s*(?:\]|】)?\s*[:：])"
    r"|(?im:^[ \t]*(?:\[|【)?(?:旁白|配音|字幕|运镜|音效|配乐|BGM|时长|narration|voice[ -]?over|camera|shot\s*\d+)\s*(?:\]|】)?\s*[:：])"
)
_QUOTE = re.compile(r'“[^”]*”|‘[^’]*’|"[^"\n]*"')
_META_FAILURE = re.compile(
    r"^(?:很?抱歉|对不起)[，,！!\s]*(?:我(?:不能|无法)|无法)[^。\n]{0,45}(?:创作|生成|提供|完成)[^。\n]{0,25}(?:故事|请求|内容)"
    r"|^(?:故事|正文|内容)(?:尚未|暂未|无法|待)(?:生成|完成|提供)"
    r"|^(?i:as an ai\b|i(?:'m| am) sorry\b)"
)


def validate_story_text(story: Any) -> str:
    """Reject malformed narration and obvious direction leakage, not judge art."""
    result = _text(story, "STORY_WRITER_CONTENT_INVALID", minimum=20, maximum=12000)
    unquoted = _QUOTE.sub(lambda match: " " * len(match.group()), result)
    if _PRODUCTION_DIRECTION.search(unquoted):
        raise ValueError("STORY_WRITER_PRODUCTION_DIRECTION_IN_TEXT")
    if "```" in result or _META_FAILURE.search(result):
        raise ValueError("STORY_WRITER_RESPONSE_NOT_STORY")
    return result


def story_text_sha256(story: str) -> str:
    """Hash canonical body text only; title changes do not invalidate media."""
    if not isinstance(story, str) or _CONTROL.search(story):
        raise ValueError("STORY_WRITER_CONTENT_INVALID")
    return hashlib.sha256(story.strip().encode("utf-8")).hexdigest()


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("STORY_WRITER_JSON_INVALID")
        result[key] = value
    return result


def _invalid_constant(_value: str) -> None:
    raise ValueError("STORY_WRITER_JSON_INVALID")


def _records(value: Any, *, kind: str, body_hash: str) -> list[dict[str, str]]:
    fields = ("name", "role", "description") if kind == "character" else ("title", "summary")
    limits = (160, 500, 1600) if kind == "character" else (160, 1600)
    id_field = f"{kind}_id"
    maximum = 16 if kind == "character" else 32
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("STORY_WRITER_CONTENT_INVALID")
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or not set(fields).issubset(item) or set(item) - {*fields, id_field}:
            raise ValueError("STORY_WRITER_CONTENT_INVALID")
        identity = item.get(id_field)
        if identity is None and id_field not in item:
            identity = f"{kind}_{body_hash[:16]}_{index:02d}"
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in seen:
            raise ValueError("STORY_WRITER_IDENTITY_INVALID")
        seen.add(identity)
        row = {id_field: identity}
        row.update({field: _text(item[field], "STORY_WRITER_CONTENT_INVALID", maximum=limit)
                    for field, limit in zip(fields, limits)})
        records.append(row)
    return records


def parse_story_writer_response(raw: str) -> dict[str, Any]:
    """Strictly parse one candidate; no repairs, fallbacks or quality scores."""
    if not isinstance(raw, str) or len(raw) > 100000:
        raise ValueError("STORY_WRITER_JSON_INVALID")
    text = raw.strip().lstrip("\ufeff")
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1)
    try:
        payload = json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("STORY_WRITER_JSON_INVALID") from exc
    expected = {"title", "story", "synopsis", "characters", "scenes"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("STORY_WRITER_CONTENT_INVALID")
    title = _text(payload["title"], "STORY_WRITER_CONTENT_INVALID", maximum=80)
    story = validate_story_text(payload["story"])
    synopsis = _text(payload["synopsis"], "STORY_WRITER_CONTENT_INVALID", maximum=1000)
    body_hash = story_text_sha256(story)
    return {
        "title": title, "story": story, "synopsis": synopsis,
        "characters": _records(payload["characters"], kind="character", body_hash=body_hash),
        "scenes": _records(payload["scenes"], kind="scene", body_hash=body_hash),
        "story_sha256": body_hash,
        "derived_from_story_sha256": body_hash,
        "character_identity_status": "suggested",
        "editorial_review": "not_performed",
    }
