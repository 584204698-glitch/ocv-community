"""Convert OCV's model-neutral motion prompt into the selected MiniMax H3 format.

The contract is intentionally isolated from the storyboard Agents.  It runs only
when a project explicitly enables it for a local ComfyUI video workflow.
"""
from __future__ import annotations

import hashlib
import base64
import io
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .gemini_client import generate_gemini_text, parse_json_response
from .video_director_contracts import NO_AUTO_SUBTITLES, enforce_no_auto_subtitles
from .video_text_policy import approved_visible_texts, visual_first_prompt_issues


H3_SKILL_SOURCE = "MiniMax-AI/MiniMax-H3 h3-prompt-writing + OCV input-mode contract"
H3_PROMPT_LANGUAGE = "zh-CN"
H3_PROMPT_CONTRACT = 19


class H3PromptValidationError(ValueError):
    """A rejected model draft, retained separately from an executable prompt."""

    def __init__(self, message, *, source_fingerprint, input_mode,
                 raw_response='', draft_prompt='', source='model'):
        message = str(message)[:2000]
        super().__init__(message)
        raw = raw_response if isinstance(raw_response, str) else ''
        draft = draft_prompt if isinstance(draft_prompt, str) else ''
        self.diagnostic = {
            'version': 1, 'source_fingerprint': source_fingerprint,
            'input_mode': input_mode, 'source': source, 'error': message,
            'raw_response': raw[:64000], 'draft_prompt': draft[:16000],
            'raw_truncated': len(raw) > 64000, 'draft_truncated': len(draft) > 16000,
        }


_SECTIONS = (
    "subject_definitions",
    "summary",
    "retention_analysis",
    "detailed_description",
    "overall_soundscape",
    "non_diegetic_music",
)
_KEYFRAME_SECTIONS = (
    "integrated_multimodal_description", "overall_soundscape", "non_diegetic_music",
)
_SUBTITLE_POLICY = (
    "Do not generate subtitles, automatic captions, lower thirds, narration transcription, title "
    "cards, explanatory overlays, or watermarks. Only exact approved short in-scene text may appear "
    "inside its assigned object or bubble; never turn it into subtitles."
)
_EFFECTS_ONLY_POLICY = (
    "Preserve the specified soundscape. Do not add spoken dialogue, narration, "
    "singing, or background music."
)
_ZH_SUBTITLE_POLICY = "不生成字幕、旁白转写、标题卡或水印；仅允许已批准的场景内短字出现在对应物体或气泡中。"
_ZH_EFFECTS_ONLY_POLICY = (
    "仅保留本镜已描述的自然环境声和动作声。旁白与角色台词由后期配音承担；"
    "视频内不生成任何语言的人声、对白、旁白、歌声或背景音乐。"
    "角色用无声表情和肢体动作表达，不做连续说话口型。"
)
_FORMATS = {
    "single_first": "h3_i2va",
    "first_last": "h3_fl2va",
    "composition_reference": "h3_ref2va",
}
_TIME_RANGE = re.compile(
    r"\[\s*(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*[-–—~至]\s*"
    r"(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*\]"
)
_SHOT = re.compile(r"\[Shot ([1-9]\d*)\]")
_CUT_TIME = re.compile(r"At (\d{2}):([0-5]\d)\.(\d{3})\b(?![.\d:])")

_SYSTEM = r"""You are OCV H3 Prompt Agent. Your only job is to convert one already-approved,
model-neutral video prompt into the selected MiniMax H3 input-mode format specified below.
Do not redesign the storyboard, alter narration boundaries, add new facts, or change the core image.

Follow the official h3-prompt-writing skill contract:
The model_neutral_video_prompt is the sole creative brief. Image pixels, approved_visible_texts,
confirmed_character_inputs and reference_assets constrain its execution; they do not authorize
additional events or revive discarded storyboard actions.
1. Follow the selected input mode's exact alignment line, section names, and timeline syntax below.
2. This installation explicitly requests Simplified Chinese section bodies (zh-CN), overriding
   the skill's English-prose recommendation. Keep the exact English section names, Picture/Subject/
   Audio/Shot labels, alignment lines, cut notation and N/A values. Write scene, action and sound
   descriptions in Chinese; approved visible scene text keeps its original language. Never invent it.
3. All actual images are attached, in reference_assets order. Inspect EVERY image's pixels before
   writing. Their tags, roles and subject mappings are authoritative. Never invent unattached pictures.
4. Describe the approved action continuously within requested_duration_seconds (4-15 seconds).
   [Shot 1] has no timestamp. Unless allow_cuts is true, use exactly one continuous [Shot 1].
   Only approved later shots begin [Shot N] At MM:SS.mmm, with strictly increasing cut times
   greater than zero and less than the requested duration. Do not create a cut at the video's end.
   Do not split action stages into additional shots or require an ending timestamp.
5. Describe composition, subjects, positions, environment, actions/state changes, camera motion,
   physical sound, and exactly when referenced content appears. Prefer specific observable actions.
   Preserve clothing, props, scenery and character identity. A later-entering subject stays offscreen
   until the approved action introduces it. References never authorize extra events, people or cuts.
   When confirmed_character_inputs has status covered or empty, it records the user's confirmed
   character-to-picture bindings. Respect its opening/later presence and actual picture labels;
   a portrait of a later entrant never puts that character into the opening shot. It is input
   evidence, not a guarantee of model output quality. Unknown or stale status supplies no cast list.
   A linked project_character_id also carries the user's saved identity constraints. Preserve those
   explicit appearance, anatomy and clothing requirements alongside the actual referenced image;
   never change anatomy or remove clothing to make an action easier. The ID itself is not a picture.
6. Do not introduce unresolved reference labels. Define any <Subject N> before using it.
7. Preserve speaker attribution and the approved order of events. A quoted belief, thought,
   stereotype, hypothetical, or example must remain visibly framed as such.
8. Audio policy: retain the requested concrete environmental and physical/action sounds in
   overall_soundscape, using 1-4 Chinese sentences in one paragraph. Do not replace those sounds
   with generic prohibitions. Write exactly N/A for overall_soundscape only when the source explicitly
   requests complete silence; preserve that silence without adding ambience or action sounds. When
   reference_audio_supplied is true, define <Audio 1> as the supplied, already-final TTS segment and
   use its exact timing, cadence, pauses, and emotional emphasis to drive visible performance; never
   transcribe it into visible text or generate replacement speech.
9. OCV adds narration separately. Do not generate spoken dialogue, narration, singing, or background
   music, even if the source requests them. Write exactly N/A in non_diegetic_music, following the
   official no-background-music format. Keep this production constraint separate from the actual
   soundscape, inside the visual description. Preserve requested natural non-verbal sounds, such as
   breathing or laughter; do not invent additional sound events.
   A source phrase such as 说出自己的判断, 对大家说 or a quoted line describes narrative meaning,
   NOT a request to speak in the generated video. Translate its intent into silent expression,
   gaze and gesture. Do not copy the line into the H3 prompt, request continuous speech-like mouth
   movements or mention an imagined speaking voice. Unless supplied reference_audio_requires_lipsync
   is true, keep the mouth naturally at rest rather than silently mouthing dialogue.
   Apply this conversion to EVERY section, including summary, subject_definitions, retention_analysis,
   detailed_description and overall_soundscape. A silent prefix does not neutralize a speaking event:
   do not carry narrative labels such as silently rehearsing a speech, introducing someone, forgetting
   the next line, or an unheard conversation into the final H3 prose. Express the same approved beat
   through observable posture, hand movement, hesitation and gaze; its verbal meaning stays in the
   separately rendered narration. For example, preserve the approved phone-at-ear pose, lowering the
   phone, walking, raising a hand, pausing and looking aside; do not describe the character as listening
   to an imagined caller or rehearsing an introduction. Phones and offscreen people never authorize
   extra voices. Do not invent replacement actions, delete an approved physical event, alter its timing,
   or remove requested non-verbal breathing/laughter. Keep legitimate approved visible scene text under
   rule 13. These are conversion instructions, not example phrases to copy into the generated prompt.
10. Preserve the lighting direction, shadow placement, exposure, contrast, and color temperature of
   <Picture 1> as closely as possible. Do not invent dramatic relighting, flicker, lens flares, or a
   day/night change unless the approved source explicitly requests it. A centered presenter, podium,
   or stage never implies theatrical lighting. Unless the approved source explicitly requests a
   lighting change or effect, do not add a spotlight, circular pool of light, radial glow, halo,
   vignette, darkened corners, or dimmed surroundings to isolate the subject. By default, keep the
   whole frame illuminated like <Picture 1>.
11. Keep the camera locked by default. Perform only camera movement explicitly written in the approved
   model-neutral video prompt; do not add push-ins, pull-outs, pans, tilts, zooms, or handheld motion.
12. The detailed description should be rich enough for H3, but every described event must fit the
   short requested duration. Do not pad it with generic cinematic adjectives.
13. approved_visible_texts is authoritative. Only those exact strings may appear as newly generated
   visible text. Narration, subtitles, intent, quoted examples, and model-neutral prompt sentences are
   semantic context, not dialogue bubbles or titles. Remove any unapproved visible dialogue instruction
   instead of translating it. Empty approved_visible_texts means generate no new visible words. Pictorial
   thought bubbles may contain concrete places, objects, or events without written labels.
   Never generate subtitles, automatic captions, lower-third captions, narration transcription, title
   cards, explanatory overlays, or watermarks. Even when approved_visible_texts is non-empty, those exact
   short strings may appear only inside their approved in-scene container and must never become subtitles.
14. Describe change rather than redundantly reconstructing the attached image. Do not restate every
   visible hairstyle, clothing color, prop, or background object inside each shot. Instead explicitly
   preserve identity, drawing style, the initial spatial layout, lighting and palette from
   <Picture 1>, then describe only what moves, appears, disappears, changes, or receives emphasis.
   Initial composition is not a constraint freezing moving subjects at their initial coordinates.
15. Each [Shot N] must follow this internal order: composition and framing; referenced subjects;
   environment/light continuity; visible action arc; camera movement; precisely triggered diegetic
   sound effects; and the screen position/time at which <Picture 1> content is used. Keep one coherent
   camera style per shot. A cut must reveal genuinely new information; otherwise prefer a locked shot
   or the single camera movement already approved by the source prompt.
16. Use concrete physical verbs, materials, directions, speeds, amplitudes and screen positions.
   Avoid empty adjectives such as cinematic, atmospheric, stunning, or dramatic. Aim for roughly
   400-900 Chinese characters when the approved action has enough events, but never pad a short 4-15 second
   shot or schedule more actions than can visibly finish.
17. Make approved translation through space observable, not just a limb cycle. For running, walking,
   driving, flying or carrying something, specify an achievable start, direction and end relative to
   existing fixed landmarks. With a locked camera the subject's whole body changes position; with
   explicitly approved tracking, ground/background parallax proves travel even if the subject stays
   centered. Do not invent tracking to conceal a frozen subject. Do not freeze the subject-landmark
   distance, slide planted feet, loop the opening pose, or reset position at the end. Stationary
   exercise or running in place remains stationary when that is the approved action.
18. Inspect the available travel space in the image. Preserve the approved route and screen direction,
   with an achievable destination before any subsequent stop, turn or gesture. In Ref2VA the framing
   may allow space for the approved movement; it must not lock the initial picture's coordinates.
   Keep approved event order and natural cause/effect; do not describe simultaneous incompatible
   poses or a long journey inside a short shot. Preserve the approved final state for the next shot.

Return only a JSON object: {"h3_prompt":"..."}. The value follows the selected mode's format.
"""


def _clean_prompt(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:text)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _image_data(image_path: Path | None, label: str = "<Picture 1>") -> tuple[dict[str, str], str]:
    if image_path is None:
        raise ValueError(f"H3 提示词转换缺少 {label} 图片")
    try:
        image_path = Path(image_path)
        raw = image_path.read_bytes()
        if not raw:
            raise ValueError("empty image")
        digest = hashlib.sha256(raw).hexdigest()
        with Image.open(io.BytesIO(raw)) as source:
            transposed = ImageOps.exif_transpose(source)
            has_alpha = transposed.mode in {"RGBA", "LA"} or (
                transposed.mode == "P" and "transparency" in transposed.info
            )
            if has_alpha:
                # Match video_studio._normalize_rgb_image: the understanding
                # model and ComfyUI must see the same visible cutout/background.
                rgba = transposed.convert("RGBA")
                flattened = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                flattened.alpha_composite(rgba)
                image = flattened.convert("RGB")
            else:
                image = transposed.convert("RGB")
            image.thumbnail((1600, 1600))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=90, optimize=True)
    except (OSError, TypeError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"H3 的 {label} 图片不存在、为空或无法解码") from exc
    return {"mime_type": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}, digest


def _prepare_images(
    image_path: Path | None, input_mode: str, last_image_path: Path | None,
    reference_images: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if input_mode not in _FORMATS:
        raise ValueError(f"不支持的 H3 图片输入模式：{input_mode}")
    if reference_images is not None and not isinstance(reference_images, list):
        raise ValueError("H3 额外参考图必须是有序列表")
    references = reference_images or []
    if len(references) > 8:
        raise ValueError("H3 最多接受 8 张额外参考图（Picture 2–9）")
    if input_mode != "composition_reference" and references:
        raise ValueError("只有构图加参考模式可使用额外参考图")
    if input_mode == "first_last" and last_image_path is None:
        raise ValueError("H3 首尾帧模式缺少尾帧图片 <Picture 2>")
    if input_mode != "first_last" and last_image_path is not None:
        raise ValueError("只有首尾帧模式可使用尾帧图片")

    specs = [{
        "path": image_path, "picture_label": "<Picture 1>", "id": "core",
        "label": "approved storyboard image",
        "role": ("composition_style_anchor_not_strict_first_frame"
                 if input_mode == "composition_reference" else "first_frame"),
    }]
    if input_mode == "first_last":
        specs.append({
            "path": last_image_path, "picture_label": "<Picture 2>", "id": "last",
            "label": "approved ending image", "role": "last_frame",
        })
    ids = set()
    for index, reference in enumerate(references, 2):
        if not isinstance(reference, dict) or reference.get("path") is None:
            raise ValueError(f"H3 的 <Picture {index}> 缺少参考图路径")
        metadata = {}
        for field in ("id", "label", "role"):
            value = reference.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"H3 的 <Picture {index}> 缺少有效 {field}")
            metadata[field] = value.strip()
        if "name" in reference:
            name = reference["name"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"H3 的 <Picture {index}> 缺少有效 name")
            metadata["name"] = name.strip()
        if metadata["id"] in ids:
            raise ValueError("H3 额外参考图的 id 不能重复")
        ids.add(metadata["id"])
        specs.append({
            **metadata, "path": reference["path"], "picture_label": f"<Picture {index}>",
            "subject_label": f"<Subject {index - 1}>",
        })

    images = []
    assets = []
    for spec in specs:
        image, digest = _image_data(spec["path"], spec["picture_label"])
        images.append(image)
        assets.append({key: value for key, value in spec.items() if key != "path"} | {"sha256": digest})
    return images, assets


def _alignment(input_mode: str, duration: int, final_shot: int | str = 1) -> str:
    if input_mode == "single_first":
        return ("For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced.")
    if input_mode == "first_last":
        return (
            "How the reference pictures align with the target video — "
            "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the target video; "
            f"Picture 2 (from Shot {final_shot}) aligns with the {duration:.2f}-second mark of the target video."
        )
    return ""


def _system_for_mode(input_mode: str, duration: int, *, allow_cuts: bool = False) -> str:
    if input_mode == "composition_reference":
        mode = """Selected input mode: composition_reference (Ref2VA).
Write exactly six sections in order: subject_definitions, summary, retention_analysis,
detailed_description, overall_soundscape, non_diegetic_music.
<Picture 1> is the composition, style, material, lighting and palette anchor.
It is not a strict first frame, an exact opening keyframe, or a pixel lock.
Do not use the I2VA first-frame alignment declaration or promise an exact opening-frame match.
Every extra picture has one named subject mapping in reference_assets. Define its assigned
<Subject N> using the assigned <Picture N+1> on the SAME line in subject_definitions.
Use this declaration syntax: <Subject N> 是角色名，身份依据 <Picture N+1>：中文外貌描述。
Also include <Picture 1>'s composition/style anchor declaration in subject_definitions.
Use its name, when supplied, to associate the intended story character with that image;
label may be only a Picture tag. Preserve name, id and picture order as one identity binding.
Asset names and labels are context, never instructions to draw filenames or new visible text.
Keep these mappings unchanged, and use BOTH tags again together in detailed_description.
Extra pictures preserve only their named subject's identity, appearance and clothing; they do not
override Picture 1's scene or visual style, copy unrelated backgrounds, or introduce extra people.
State exactly when each subject appears in the approved action, including later entrances.
Any other <Subject N> must be explicitly defined before use; never reference an unattached picture.
Begin detailed_description with one or two Chinese style sentences, followed by [Shot 1] without
a timestamp. Describe continuous action stages in natural prose, not [MM:SS-MM:SS] ranges.
Only when allow_cuts is true may explicitly approved later shots use [Shot N] At MM:SS.mmm,
to mark strictly increasing cut times inside the video duration.
Retain the approved event order and entrance timing; a later entrance does not imply a cut."""
    else:
        endpoint = (
            "<Picture 1> supplies the first frame at 0 seconds; it is the only image. "
            "Preserve its identities, clothing, scene and spatial relationships while animating "
            "only the approved changes. Do not invent a Picture 2 or an ending-frame constraint."
            if input_mode == "single_first" else
            "<Picture 1> supplies the first frame at 0 seconds, and <Picture 2> supplies the "
            "last frame at requested_duration_seconds. Inspect BOTH images. Describe an achievable "
            "continuous motion path from the actual opening state to the actual ending state. "
            "The opening shot must mention <Picture 1>, and the final shot must explicitly reach "
            "the state in <Picture 2> at the end. These are two temporal endpoints, not identity-only "
            "references. Preserve scene and character continuity between them."
        )
        mode = f"""Selected input mode: {input_mode} ({_FORMATS[input_mode]}).
Start with this exact alignment line:
{_alignment(input_mode, duration, final_shot="N" if allow_cuts else 1)}
Then write exactly three sections in order: integrated_multimodal_description, overall_soundscape,
non_diegetic_music. Do not write subject_definitions, summary, retention_analysis or detailed_description.
The integrated_multimodal_description must start with [Shot 1].
{endpoint}
Do not add a timestamp to [Shot 1]. Describe its continuous action and final state in natural prose.
Use exactly one [Shot 1] unless allow_cuts is true. Only approved later shots begin with
[Shot N] At MM:SS.mmm, where N is sequential and cut times increase strictly inside the duration.
For first_last with approved cuts, replace N in the alignment line with the actual final shot number.
There is no required ending timestamp: the first/last alignment line already sets the two endpoints.
Use picture tags and ordinary subject names; do not introduce <Subject N> tags in this format.
Do not use bracketed time ranges or Ref2VA composition-anchor disclaimers.
Preserve the approved event order; add no unapproved cuts."""
    return _SYSTEM + "\n\n" + mode


def _section_bounds(prompt: str) -> dict[str, tuple[int, int]]:
    names = "|".join(dict.fromkeys((*_SECTIONS, *_KEYFRAME_SECTIONS)))
    matches = list(re.finditer(rf"(?im)^[ \t]*({names})[ \t]*:[ \t]*\n?", prompt))
    return {
        match.group(1).lower(): (
            match.end(), matches[index + 1].start() if index + 1 < len(matches) else len(prompt)
        )
        for index, match in enumerate(matches)
    }


def _uses_chinese_prose(prompt: str) -> bool:
    """Localize inserted policy prose without rewriting legacy English drafts."""
    sections = _section_bounds(prompt)
    name = "detailed_description" if "detailed_description" in sections else "integrated_multimodal_description"
    body = _section_body(prompt, name) if name in sections else prompt
    chinese = len(re.findall(r"[\u4e00-\u9fff]", body))
    return chinese >= 20 and chinese > len(re.findall(r"[a-zA-Z]", body)) / 2


def _insert_in_section(prompt: str, section: str, sentence: str) -> str:
    bounds = _section_bounds(prompt)
    if section not in bounds:
        return prompt
    body_start, _ = bounds[section]
    if sentence in _section_body(prompt, section):
        return prompt
    return prompt[:body_start] + sentence.strip() + "\n" + prompt[body_start:]


def _section_body(prompt: str, section: str) -> str:
    bounds = _section_bounds(prompt)
    if section not in bounds:
        return ""
    start, end = bounds[section]
    return prompt[start:end]


def _repair_section_structure(prompt: str, input_mode: str) -> str:
    """Normalize formatting only; missing visual content must never be invented."""
    prompt = _clean_prompt(prompt)
    names = tuple(dict.fromkeys((*_SECTIONS, *_KEYFRAME_SECTIONS)))
    for name in names:
        words = re.escape(name).replace("_", r"[\s_-]+")
        prompt = re.sub(
            rf"(?im)^[ \t]*(?:[#>*-]+[ \t]*)?(?:\*\*)?{words}(?:\*\*)?[ \t]*:?[ \t]*$",
            f"{name}:", prompt,
        )
    # Upstream 2d26af9: providers may return all headings on one line.
    # Normalize separators without inventing missing creative content.
    headings = '|'.join(re.escape(name).replace('_', r'[ \t_-]+') for name in names)
    prompt = re.sub(
        rf'(?i)(?<![\w])\s*(?:\*\*)?({headings})(?:\*\*)?[ \t]*:\s*',
        lambda match: '\n\n' + re.sub(r'[ \t-]+', '_', match.group(1).lower()) + ':\n',
        prompt,
    ).strip()
    for name in names:
        if len(re.findall(rf'(?im)^\s*{name}\s*:', prompt)) > 1:
            raise ValueError(f'H3 转换结果包含重复的 {name} 段落，请重新转换')
    matches = list(re.finditer(rf"(?im)^[ \t]*({'|'.join(names)})[ \t]*:", prompt))
    expected = _SECTIONS if input_mode == "composition_reference" else _KEYFRAME_SECTIONS
    if [match.group(1).lower() for match in matches] != list(expected):
        raise ValueError(f"H3 转换结果不符合 {input_mode} 的段落结构或顺序")
    if any(not _section_body(prompt, name).strip() for name in expected):
        raise ValueError("H3 转换结果含有空段落")
    if input_mode == "composition_reference" and prompt[:matches[0].start()].strip():
        raise ValueError("H3 构图加参考模式不能附带首帧或首尾帧对齐声明")
    return prompt


def _normalize_labels(prompt: str) -> str:
    for kind in ("Picture", "Subject"):
        alias = "(?:picture|image)" if kind == "Picture" else "subject"
        prompt = re.sub(rf"<\s*{alias}\s*(\d+)\s*>", lambda m: f"<{kind} {int(m[1])}>", prompt, flags=re.I)
        prompt = re.sub(rf"\[\s*{alias}\s*(\d+)\s*\]", lambda m: f"<{kind} {int(m[1])}>", prompt, flags=re.I)
        prompt = re.sub(rf"(?<![\w<]){alias}\s*(\d+)(?![\w>])", lambda m: f"<{kind} {int(m[1])}>", prompt, flags=re.I)
    return prompt


def _cuts_requested(text: str) -> bool:
    """Only explicit positive cut directions can authorize additional shots."""
    pattern = re.compile(
        r"\b(?:camera|shot)\s+(?:cuts?|transitions?|changes?|switches?)\s+to\b|"
        r"(?:^|\n)\s*cut\s+to\b|\bcut\s+to\s+(?:an?\s+|the\s+)?"
        r"(?:close[- ]?up|wide|medium|another|next)\b|"
        r"\[Shot (?:[2-9]\d*|1\d+)\]|"
        r"切镜|切到|切至|切换(?:到|镜头|视角|机位|画面)|转场", re.I,
    )
    for clause in re.split(r"[.;\n。；]+|\b(?:but|however|yet)\b|但是|但", text, flags=re.I):
        for match in pattern.finditer(clause):
            prefix = clause[:match.start()]
            if not re.search(r"\b(?:no|not|never|without|avoid|forbid)\b|不|无|禁止|避免", prefix, re.I):
                return True
    return False


def _validate_timeline(detail: str, duration: int, input_mode: str, *, allow_cuts: bool) -> int:
    """Validate official shot/cut notation without inventing action timestamps."""
    shots = list(_SHOT.finditer(detail))
    if not shots or [int(shot[1]) for shot in shots] != list(range(1, len(shots) + 1)):
        raise ValueError("H3 镜头编号必须从 [Shot 1] 开始并连续递增")
    if len(re.findall(r"\[\s*Shot\b[^\]]*\]", detail, re.I)) != len(shots):
        raise ValueError("H3 镜头标签格式无效")
    if input_mode != "composition_reference" and detail[:shots[0].start()].strip():
        raise ValueError("H3 首帧/首尾帧描述必须从 [Shot 1] 开始")
    if _TIME_RANGE.search(detail) or re.search(r"\[\s*\d{1,2}:\d{1,2}(?:\.\d+)?\s*\]", detail):
        raise ValueError("H3 官方镜头描述不使用时间范围标签；连续动作应写在同一镜头内")
    first_body = detail[shots[0].end():shots[1].start() if len(shots) > 1 else len(detail)]
    if re.match(r"\s*(?:At\s+)?\d{1,2}:\d", first_body, re.I):
        raise ValueError("H3 官方 [Shot 1] 开场不添加时间戳")

    cut_spans = []
    previous = 0.0
    for index, shot in enumerate(shots[1:], 1):
        end = shots[index + 1].start() if index + 1 < len(shots) else len(detail)
        body = detail[shot.end():end]
        leading = len(body) - len(body.lstrip())
        stamp = _CUT_TIME.match(body, leading)
        if stamp is None:
            raise ValueError("H3 后续镜头必须以 [Shot N] At MM:SS.mmm 标明切镜时间")
        seconds = int(stamp[1]) * 60 + int(stamp[2]) + int(stamp[3]) / 1000
        if not previous < seconds < duration:
            raise ValueError("H3 切镜时间必须严格递增，且大于 0、小于视频时长")
        previous = seconds
        cut_spans.append((shot.end() + stamp.start(), shot.end() + stamp.end()))
    clocks = list(re.finditer(r"\bAt\s+\d{1,2}:\d[^\s,;]*", detail, re.I))
    if len(clocks) != len(cut_spans) or any(
        match.start() != span[0] for match, span in zip(clocks, cut_spans)
    ):
        raise ValueError("H3 时间戳只用于后续镜头的切镜位置，不能插入开场或结尾动作时间轴")
    if not allow_cuts and (len(shots) > 1 or _cuts_requested(detail)):
        raise ValueError("本镜未批准切镜，H3 必须保持单个连续 [Shot 1]")
    for stamp in re.finditer(r"\b(?:at|by|after)\s+(\d+(?:\.\d+)?)\s*seconds?\b", detail, re.I):
        if float(stamp[1]) > duration:
            raise ValueError("H3 动作描述的指定时间超出视频时长")
    if input_mode != "composition_reference" and "<Picture 1>" not in first_body:
        raise ValueError("H3 开场镜头没有对应首帧 <Picture 1>")
    if input_mode == "first_last":
        final_body = detail[shots[-1].end():]
        ending = any(
            "<Picture 2>" in sentence
            and re.search(r"\b(?:end(?:ing)?|last|final|converg\w*|settle\w*|finish\w*|land\w*)\b|尾帧|结束|最终|收敛|结尾", sentence, re.I)
            for sentence in re.split(r"[.;\n。；]+", final_body)
        )
        if not ending:
            raise ValueError("H3 最后镜头没有明确收敛到尾帧 <Picture 2>")
    return len(shots)


def _normalize_reference_definitions(prompt: str) -> str:
    """Normalize explicit declarations without inferring names or picture bindings."""
    start, end = _section_bounds(prompt)["subject_definitions"]
    definitions = prompt[start:end]
    # Observed provider form: <Subject 1> NAME, identified by <Picture 2>: ...
    # Only add the copula to this complete, same-line declaration. Bare subject
    # mentions, conflicting bindings and duplicates still face role validation.
    definitions = re.sub(
        r"(?m)^([ \t]*)(<Subject \d+>)[ \t]+([^<>\r\n:]+?),[ \t]*"
        r"identified by[ \t]+(<Picture \d+>)[ \t]*:",
        lambda match: (match[0] if re.match(r"(?:is|refers to|represents)\b", match[3])
                       else f"{match[1]}{match[2]} is {match[3]}, identified by {match[4]}:"),
        definitions,
    )
    if "<Picture 1>" not in definitions:
        # This role comes from the selected transport mode, not model-authored
        # scene content. The action must still explicitly use the actual anchor.
        definitions = (("<Picture 1> 是构图、画风、材质、光线和配色参考。\n" if _uses_chinese_prose(prompt) else
                        "<Picture 1> is the composition, style, material, lighting and palette anchor.\n")
                       + definitions)
    return prompt[:start] + definitions + prompt[end:]


def _assert_reference_roles(prompt: str, assets: list[dict[str, str]], input_mode: str) -> None:
    expected = {asset["picture_label"] for asset in assets}
    used = set(re.findall(r"<Picture \d+>", prompt))
    if used != expected:
        raise ValueError("H3 图片标签与实际传入图片不一致："
                         + "、".join(sorted(used.symmetric_difference(expected))))
    subjects = set(re.findall(r"<Subject \d+>", prompt))
    if input_mode != "composition_reference":
        if subjects:
            raise ValueError("H3 首帧/首尾帧格式不能包含未定义 Subject 标签")
        if re.search(r"\bnot\s+(?:an?\s+|the\s+)?(?:strict|exact)\s+(?:first|opening)[ -]?frame", prompt, re.I):
            raise ValueError("H3 首帧/首尾帧格式不能把实际首帧改成非首帧构图参考")
        if input_mode == "first_last" and re.search(
            r"<Picture 2>[^.\n]*\b(?:identity[- ]only|identity only|not\s+(?:a\s+|the\s+)?last frame)\b",
            prompt, re.I,
        ):
            raise ValueError("H3 尾帧不能被改成身份参考图")
        return
    definitions = _section_body(prompt, "subject_definitions")
    declarations = list(re.finditer(r"(<Subject \d+>)\s*(?:is\b|refers to\b|represents\b|是|代表|[:：=])", definitions))
    defined = {match[1] for match in declarations}
    if subjects - defined:
        raise ValueError("H3 转换结果存在未定义的 Subject 标签")
    definition_start = _section_bounds(prompt)["subject_definitions"][0]
    if any(prompt.find(match[1]) != definition_start + match.start() for match in declarations):
        raise ValueError("H3 Subject 必须先定义且不能重复定义")
    detail = _section_body(prompt, "detailed_description")
    if "<Picture 1>" not in definitions or "<Picture 1>" not in detail:
        raise ValueError("H3 构图锚点必须同时出现在主体定义与动作描述")
    for asset in assets[1:]:
        picture, subject = asset["picture_label"], asset["subject_label"]
        definition_lines = [
            line for line in definitions.splitlines()
            if re.search(rf"{re.escape(subject)}\s*(?:is\b|refers to\b|represents\b|是|代表|[:：=])", line)
        ]
        mapped = any(picture in line for line in definition_lines)
        playback = any(picture in line and subject in line for line in detail.splitlines())
        if subject not in defined or not mapped or not playback:
            raise ValueError(f"H3 缺少 {subject} 与 {picture} 的明确角色映射")
        # A reference cannot quietly claim two different named identities.
        for line in definition_lines:
            pictures = set(re.findall(r"<Picture \d+>", line)) - {"<Picture 1>"}
            if pictures != {picture}:
                raise ValueError(f"H3 的 {subject} 与传入参考图绑定不一致")


def _assert_no_strict_reference_frame(prompt: str) -> None:
    for clause in re.split(r"[。；;\n]|但是|但|然而", prompt):
        # Mask only the negated promise; a neighboring affirmative remains visible.
        clause = re.sub(r"(?:不是|并非|不作为|不要求|不做)(?:严格首帧|真实首帧|像素锁定|逐像素锁定)", "", clause)
        if re.search(r"(?:必须|保证|保持|使用|作为|是)(?:严格首帧|像素锁定|逐像素锁定)|首帧(?:必须)?(?:完全一致|逐像素一致)", clause):
            raise ValueError("H3 构图参考不能承诺严格首帧或像素锁定")
    normalized = re.sub(r"\s+", " ", prompt).lower()
    # Hyphenated opening-frame has the same scope as opening frame. Normalize
    # that noun only; a later affirmative promise must still be checked.
    normalized = re.sub(r"\bopening-frame\b", "opening frame", normalized)
    clauses = re.split(r"[.!?;]+|\b(?:but|yet|however|nevertheless)\b", normalized)
    strict = (
        r"\b(?:exact|strict|pixel[- ](?:perfect|locked|level))\s+(?:opening|first)[ -]?frame\b",
        r"\b(?:opening|first)[ -]?frame\s+(?:must\s+)?(?:exactly\s+match|lock|is\s+locked)",
        r"\b(?:fully|exactly)\s+(?:reference[ds]?|preserv(?:e|ed)|reproduc(?:e|ed))\s+"
        r"(?:as\s+)?(?:the\s+)?(?:opening|first)[ -]?frame",
        r"\bmust\s+(?:exactly\s+)?(?:match|reproduce|preserve)\s+(?:the\s+)?"
        r"(?:opening|first)[ -]?frame",
        r"\b(?:is|be|remain|enforce|guarantee)\s+(?:an?\s+)?pixel[- ](?:perfect|locked|lock)\b",
    )
    for clause in clauses:
        # Mask only the negated promise itself; an unrelated "not" must not
        # excuse a later affirmative pixel-lock instruction in the same clause.
        clause = re.sub(
            r"\b(?:not(?:\s+as)?|rather\s+than|"
            r"without\s+(?:treating|using|regarding)\s+(?:it|this|that|<picture\s+\d+>)\s+as)"
            r"\s+(?:(?:an?|the)\s+)?(?:mandatory\s+)?"
            r"(?:exact|strict|pixel[- ](?:perfect|locked|level))\s+(?:opening|first)[ -]?frame\b",
            " non-strict reference policy ", clause,
        )
        for permitted in (
            "not a strict first frame", "not an exact first frame", "not an exact opening keyframe",
            "not an exact opening frame", "not a pixel lock", "not pixel-locked",
            "not required to be the strict first frame",
            "not required to be reproduced as the exact first frame",
            "rather than a mandatory exact opening frame",
            "rather than a strict first frame",
            "without requiring an exact first-frame match",
            "does not guarantee an exact opening frame",
            "without claiming exact first-frame pixels",
        ):
            clause = clause.replace(permitted, " non-strict reference policy ")
        if any(re.search(pattern, clause) for pattern in strict):
            raise ValueError("H3 构图加参考模式不能承诺严格首帧或像素锁定")


def _append_description_policy(prompt: str, detail_section: str, policy: str) -> str:
    # Preserve the official opening: either [Shot 1] or Ref2VA's style preface.
    start, end = _section_bounds(prompt)[detail_section]
    body = prompt[start:end].rstrip()
    if policy not in body:
        prompt = prompt[:start] + body + "\n" + policy + "\n\n" + prompt[end:]
    return prompt.strip()


def _append_subtitle_policy(prompt: str, detail_section: str) -> str:
    return _append_description_policy(prompt, detail_section,
                                      _ZH_SUBTITLE_POLICY if _uses_chinese_prose(prompt) else _SUBTITLE_POLICY)


def prepare_h3_video_prompt(prompt: str, *, effects_only: bool = False) -> str:
    """Preserve authored audio unless effects-only H3 preparation is requested."""
    original = str(prompt or "").strip()
    prompt = original
    suffix = "\n" + NO_AUTO_SUBTITLES
    while prompt.endswith(suffix):
        prompt = prompt[:-len(suffix)].rstrip()
    names = "|".join(dict.fromkeys((*_SECTIONS, *_KEYFRAME_SECTIONS)))
    headings = tuple(match.group(1).lower() for match in re.finditer(
        rf"(?im)^[ \t]*({names})[ \t]*:[ \t]*\n?", prompt,
    ))
    if (headings not in (_KEYFRAME_SECTIONS, _SECTIONS)
            or any(not _section_body(prompt, name).strip() for name in headings)):
        if effects_only:
            raise ValueError("H3 声音设置需要完整、非空且顺序正确的提示词；请启用 H3 提示词转换，或按官方三段/六段格式补全")
        return enforce_no_auto_subtitles(original)
    detail_section = "detailed_description" if headings == _SECTIONS else "integrated_multimodal_description"
    if effects_only:
        _assert_no_generated_audio("\n".join(
            _section_body(prompt, name) for name in ("summary", detail_section)
            if name in _section_bounds(prompt)
        ))
        return _enforce_output_policy(prompt, detail_section)
    return _append_subtitle_policy(prompt, detail_section)


def _enforce_output_policy(prompt: str, detail_section: str) -> str:
    soundscape = _section_body(prompt, "overall_soundscape").strip()
    _assert_no_generated_audio(soundscape)
    # Official overall_soundscape N/A means complete silence.  Never replace it
    # or concrete authored effects with generic instructions to invent ambience.
    if re.fullmatch(r"N/A\.?", soundscape, re.I):
        start, end = _section_bounds(prompt)["overall_soundscape"]
        prompt = prompt[:start] + "N/A\n\n" + prompt[end:]
    start, end = _section_bounds(prompt)["non_diegetic_music"]
    prompt = prompt[:start] + "N/A\n\n" + prompt[end:]
    prompt = _append_description_policy(prompt, detail_section,
                                       _ZH_EFFECTS_ONLY_POLICY if _uses_chinese_prose(prompt) else _EFFECTS_ONLY_POLICY)
    return _append_subtitle_policy(prompt, detail_section)


def _assert_no_chinese_generated_audio(description: str) -> None:
    """Check Chinese audio directions; a prohibition cannot hide a neighboring voice request."""
    # Judge each audio predicate locally. Whole-clause noun allowlists reject
    # harmless new subjects/adverbs/visual exclusions emitted by the director.
    # A negation applies only immediately before this predicate; it can never
    # excuse another affirmative predicate later in the same sentence.
    negation = r"(?:不(?:应该|应|允许|可以|能|会|要|得|做|作)?|禁止|无需|避免|没有|未|无)"
    negative_prefix = re.compile(negation + r"(?:连续)?$")
    double_negative = re.compile(negation + r"(?:是)?" + negation + r"(?:连续)?$|并非" + negation + r"(?:连续)?$")
    # A speech action may be a noun in a prohibited output list, e.g.
    # "不得生成对白、说话口型、新增可见文字". Scope only the list item;
    # an affirmative predicate elsewhere in the list is still checked below.
    nominal_action = re.compile(r"(?:说话口型(?:动作)?|说话动作|开口说话|说话|唱歌|歌唱|发言)(?=$|[、和或及])")
    output_list_prefix = re.compile(r"^(?P<head>.*?)(?:生成|添加|产生)[^。；;，,\n]*[、和或及](?:连续)?$")
    absent_list_prefix = re.compile(
        r"^(?P<head>.*?(?:没有|无))"
        r"(?:(?:新文字|文字|字幕|标题卡|水印|对白|旁白|人声|台词|歌声|歌唱|唱歌|说话口型|背景音乐|配乐)[、和或及])+(?:连续)?$")
    positive = re.compile(
        r"(?:(?:开口)?(?:说出|说道|说着|说话|发言|念出|读出|喊出|唱出|唱歌|歌唱)|开口)|"
        r"(?:说|回答|回应|喊|唱)[：:‘’“”\"']|"
        r"(?:生成|添加|播放|响起|传来|听到|听见|伴随|配上|发出)(?:了|着|一段|一些|轻柔的|清晰的|英文的?|中文的?|英语的?|汉语的?|任何语言的)*"
        r"(?:人声|对白|台词|旁白|歌声|语音|背景音乐|配乐|音乐|BGM)|"
        r"(?:背景音乐|配乐|旁白|对白|歌声|人声)(?:开始|响起|响着|播放|传来|伴随)", re.I,
    )
    for clause in re.split(r"[。；;\n，,]+|但是|然而|但|同时|而且", description):
        clause = clause.strip()
        if not clause:
            continue
        if re.fullmatch(r"(?:原文中的?|故事中的?)?(?:旁白|台词|对白|旁白与角色台词)(?:由|在)(?:后期|后期配音)(?:单独)?(?:承担|合成|配音|添加)", clause):
            continue
        for predicate in positive.finditer(clause):
            prefix = clause[:predicate.start()]
            if negative_prefix.search(prefix) and not double_negative.search(prefix):
                continue
            if re.search(r"不要求(?:连续)?$", prefix) and not re.search(r"(?:不是|不能|并非)不要求(?:连续)?$", prefix):
                continue
            output_list = output_list_prefix.match(prefix) or absent_list_prefix.match(prefix)
            if output_list and nominal_action.match(clause[predicate.start():]):
                head = output_list.group('head')
                if negative_prefix.search(head) and not double_negative.search(head):
                    continue
            raise ValueError("H3 中文描述包含生成语音或背景音乐指令；旁白应在后期合成，请改用无声表情和动作")


def _assert_no_generated_audio(description: str) -> None:
    # Reject positive audio directions outside the two rewritten audio sections.
    # Separate contrast clauses so "no speech, but the narrator says..." fails.
    description = description.replace(_EFFECTS_ONLY_POLICY, "").replace(_ZH_EFFECTS_ONLY_POLICY, "")
    _assert_no_chinese_generated_audio(description)
    description = re.sub(r"\bnon[- ]speaking\b", "unvoiced", description, flags=re.I)
    # Birdsong is a natural non-verbal sound.  Only exempt an explicit bird
    # subject beginning its own sentence; a compound human/bird subject or a
    # bird performing lyrics must still be rejected.  The authored text stays
    # unchanged: this normalization is solely for the conflict check.
    def natural_bird_call(match):
        remainder = match[2]
        if re.search(r"\b(?:words?|lyrics?|song|melody|chorus|stage|microphone)\b|[\"“]", remainder, re.I):
            return match[0]
        return match[1] + "chirp" + remainder

    description = re.sub(
        r"((?:^|[.;\n])\s*(?:(?:a|the|some|several)\s+)?(?:birds?|songbirds?)\s+(?:(?:is|are)\s+)?)"
        r"sing(?:s|ing)?\b([^.;\n]*)", natural_bird_call, description, flags=re.I,
    )
    # Keep explicit negative lists intact when splitting at commas.  Otherwise
    # "No speech, narration or singing" looks like a positive singing request.
    # A single negative list can mix audio and visual nouns.  Its items must
    # remain recognized noun phrases, never a new subject/predicate such as
    # "the woman sings"; that positive instruction still needs rejection.
    # Require the complete noun list to end the sentence. Masking just a prefix
    # could swallow a new compound subject: "Without speech, background music
    # and footsteps fill the room" still contains an affirmative music request.
    excluded_item = (
        r"(?:human speech|spoken dialogue|speech|voices?|narration|dialogue|singing|talking|"
        r"visible narration text|"
        r"musical tones|background music|music|vocalizations?|humming|chanting|crowd voices|vocal reactions|"
        r"cuts?|(?:new|additional) characters|delivery|long-distance travel|transitions?|"
        r"captions?|subtitles?|titles?|overlays?|watermarks?|camera movement|noise|"
        r"(?:(?:speech|thought|dialogue)\s+)?bubbles?|title cards?|subtitle bars?|"
        r"(?:other\s+)?(?:new(?:ly)?\s+)?(?:generated\s+)?(?:visible\s+|legible\s+)?(?:text|writing|words))"
    )
    excluded_list = (
        rf"{excluded_item}(?:\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+){excluded_item})*"
    )
    # "No dialogue, singing, or music is produced" negates the whole noun
    # list. Only this explicit no-subject plus a production passive is masked;
    # "Without speech, singing is generated" remains an affirmative request.
    description = re.sub(
        rf"\bno\s+(?:any\s+)?{excluded_list}\s+(?:is|are)\s+"
        r"(?:produced|generated|added|included|created|synthesized)(?=\s*(?:[.;\n]|$))",
        "", description, flags=re.I,
    )
    description = re.sub(
        rf"\b(?:no|without|avoid|omit|exclude|"
        rf"(?:do\s+not|don['’]t|never|must\s+not)\s+(?:add|generate|include|produce|create|play|synthesize))"
        rf"\s+(?:any\s+)?{excluded_list}"
        r"(?=\s*(?:[.;\n]|$))", "", description, flags=re.I,
    )
    clauses = re.split(r"[.,;\n]+|\b(?:but|however|yet|and|while)\b", description, flags=re.I)
    speech = (
        r"<d>\s*\[",
        r"\b(?:says?|speaks?|speaking|sings?|singing|replies)\b",
        r"\b(?:whispers?|whispering|shouts?|shouting|chants?|chanting)\s+(?:the\s+)?(?:words?|dialogue|lyrics?|line)\b",
        r"\b(?:narration|dialogue|voice[ -]?over|spoken words|crowd voices)\s+(?:begins?|plays?|is heard)\b",
        r"\b(?:add|generate|include|play)\s+(?:a\s+|the\s+)?(?:narration|dialogue)\b",
    )
    music = (
        r"\b(?:background music|soundtrack|musical accompaniment)\b",
        r"\b(?:music|soundtrack|melody|score|piano)\s+(?:plays?|swells?|begins?|accompanies|fades? in)\b",
        r"\b(?:add|generate|include|play)\s+(?:a\s+|the\s+)?(?:background music|BGM)\b",
    )
    for clause in clauses:
        if re.search(r"\b(?:no|not|never|without|avoid)\b", clause, re.I):
            continue
        audible_speech = not re.search(r"\b(?:silent(?:ly)?|mute[ds]?)\b", clause, re.I)
        if (any(re.search(pattern, clause, re.I) for pattern in music)
                or (audible_speech and any(re.search(pattern, clause, re.I) for pattern in speech))):
            raise ValueError("H3 动作描述包含生成语音或背景音乐指令；旁白应在后期合成")


def _enforce_reference_audio(prompt: str, detail_section: str, *, reference_audio: bool, lipsync: bool) -> str:
    if not reference_audio:
        if re.search(r"<Audio \d+>", prompt):
            raise ValueError("H3 转换结果引用了未提供的音频")
        return prompt
    definition = (
        "<Audio 1> is the supplied TTS performance reference, used only for timing, "
        "cadence, pauses and emotional emphasis; it must never become generated speech."
    )
    direction = (
        "Match visible mouth movements and silent performance to <Audio 1>."
        if lipsync else "Use <Audio 1> for action timing and emotional cadence only; mouth lip-sync is not required."
    )
    if _uses_chinese_prose(prompt):
        definition = "<Audio 1> 是已提供的后期配音节奏参考，仅用于动作时机、停顿和情绪，不生成替代人声。"
        direction = ("无声口型与表演对齐 <Audio 1>。" if lipsync else
                     "仅用 <Audio 1> 对齐动作节奏与情绪，角色保持自然表情，不做连续说话口型。")
    section = "subject_definitions" if detail_section == "detailed_description" else detail_section
    if section == "subject_definitions":
        prompt = _insert_in_section(prompt, section, definition)
    else:
        direction = definition + " " + direction
    start, end = _section_bounds(prompt)[detail_section]
    if direction not in prompt[start:end]:
        prompt = prompt[:end].rstrip() + "\n" + direction + "\n\n" + prompt[end:]
    if set(re.findall(r"<Audio \d+>", prompt)) - {"<Audio 1>"}:
        raise ValueError("H3 转换结果引用了未提供的音频")
    return prompt


def _validate(
    prompt: str, duration: int, *, reference_audio: bool = False, lipsync: bool = False,
    input_mode: str = "single_first", image_assets: list[dict[str, str]] | None = None,
    motion_plan: Any = None, allow_cuts: bool = False,
) -> str:
    if input_mode not in _FORMATS:
        raise ValueError(f"不支持的 H3 图片输入模式：{input_mode}")
    assets = image_assets or [{"picture_label": "<Picture 1>"}]
    prompt = _repair_section_structure(prompt, input_mode)
    # The keyframe alignment line is deterministic, not authored scene content.
    if input_mode != "composition_reference":
        first_section = re.search(r"(?im)^[ \t]*integrated_multimodal_description[ \t]*:", prompt)
        prompt = prompt[first_section.start():]
    prompt = _normalize_labels(prompt)
    if input_mode == "composition_reference":
        prompt = _normalize_reference_definitions(prompt)
    _assert_reference_roles(prompt, assets, input_mode)
    detail_section = "detailed_description" if input_mode == "composition_reference" else "integrated_multimodal_description"
    final_shot = _validate_timeline(
        _section_body(prompt, detail_section), duration, input_mode, allow_cuts=allow_cuts,
    )
    if input_mode == "composition_reference":
        _assert_no_strict_reference_frame(prompt)
        policy = (
            "<Picture 1> is the composition, style, material, lighting and palette anchor; "
            "it is not a strict first frame or a pixel lock. Extra pictures control only their named "
            "subjects' identities and clothing; do not copy unrelated backgrounds or introduce "
            "unapproved people. Preserve approved entrance times and event order."
        )
        if _uses_chinese_prose(prompt):
            policy = (
                "<Picture 1> 是构图、画风、材质、光线和配色的参考，不是严格首帧，也不要求像素锁定。"
                "额外图片仅约束对应角色的身份、外貌与服装；不复制其背景，不增加无关人物。"
                "保持已批准的入场时机和事件顺序；起始空间布局不限制既定动作中的真实位移。"
            )
        prompt = _insert_in_section(prompt, "retention_analysis", policy)
    else:
        if input_mode == "first_last":
            start, end = _section_bounds(prompt)[detail_section]
            convergence = (
                "Preserve one continuous achievable motion path from <Picture 1> and progressively "
                f"converge to <Picture 2> at {duration:.2f} seconds, the final endpoint."
            )
            if _uses_chinese_prose(prompt):
                convergence = f"从 <Picture 1> 沿可实现的连续动作路径逐步过渡，并在第 {duration:.2f} 秒结束时到达 <Picture 2> 的尾帧状态。"
            if convergence not in prompt[start:end]:
                prompt = prompt[:end].rstrip() + "\n" + convergence + "\n\n" + prompt[end:]
    # Check authored prose before adding the mandatory policies.
    authored_visual = "\n".join(
        _section_body(prompt, name)
        for name in ("summary", detail_section)
        if name in _section_bounds(prompt)
    )
    _assert_no_generated_audio(authored_visual)
    prompt = _enforce_reference_audio(prompt, detail_section, reference_audio=reference_audio, lipsync=lipsync)
    prompt = _enforce_output_policy(prompt, detail_section)
    if input_mode != "composition_reference":
        prompt = _alignment(input_mode, duration, final_shot=final_shot) + "\n\n" + prompt
    issues = visual_first_prompt_issues(_section_body(prompt, detail_section), motion_plan)
    if issues:
        raise ValueError("H3 转换结果重新加入了未选用的画中文字：" + "；".join(issues))
    if not 500 <= len(prompt) <= 16000:
        raise ValueError("H3 转换结果长度异常")
    return prompt


def source_fingerprint(
    shot: dict[str, Any], duration: int, *, reference_audio: bool = False, lipsync: bool = False,
    image_digest: str = "", input_mode: str = "single_first",
    image_assets: list[dict[str, str]] | None = None,
) -> str:
    source = {
        "video_prompt": str(shot.get("video_prompt") or ""),
        "approved_visible_texts": approved_visible_texts(shot.get("motion_plan")),
        "duration": duration,
        "reference_audio": reference_audio,
        "reference_audio_lipsync": bool(reference_audio and lipsync),
        "input_mode": input_mode,
        "core_image_digest": image_digest,
        "image_assets": image_assets or [],
        "confirmed_character_inputs": shot.get('_confirmed_character_inputs'),
        "skill": H3_SKILL_SOURCE,
        "prompt_language": H3_PROMPT_LANGUAGE,
        "contract": H3_PROMPT_CONTRACT,
    }
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def convert_for_h3(
    shot: dict[str, Any], duration: int, *, reference_audio: bool = False,
    lipsync: bool = False, image_path: Path | None = None, input_mode: str = "single_first",
    last_image_path: Path | None = None, reference_images: list[dict[str, Any]] | None = None,
    previous_diagnostic: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return a validated mode-specific H3 prompt and its complete input fingerprint."""
    shot.pop('_h3_diagnostic_reused', None)
    if isinstance(duration, bool) or not isinstance(duration, int) or not 4 <= duration <= 15:
        raise ValueError("H3 仅接受 4～15 秒的整数镜头时长")
    images, assets = _prepare_images(image_path, input_mode, last_image_path, reference_images)
    image_digest = assets[0]["sha256"]
    fingerprint = source_fingerprint(
        shot, duration, reference_audio=reference_audio, lipsync=lipsync,
        image_digest=image_digest, input_mode=input_mode, image_assets=assets,
    )
    # Finalized video prose owns the creative brief. Old storyboard fields may
    # describe discarded actions after a replan and must not authorize cuts.
    allow_cuts = _cuts_requested(str(shot.get("video_prompt") or ""))
    validation = {
        "reference_audio": reference_audio, "lipsync": lipsync, "input_mode": input_mode,
        "image_assets": assets, "motion_plan": shot.get("motion_plan"), "allow_cuts": allow_cuts,
    }
    cached = str(shot.get("h3_prompt") or "").strip()
    if cached and shot.get("h3_prompt_source") == fingerprint:
        try:
            return _validate(cached, duration, **validation), fingerprint
        except ValueError as exc:
            raise H3PromptValidationError(str(exc), source_fingerprint=fingerprint,
                input_mode=input_mode, draft_prompt=cached, source='cache') from exc
    # On an explicit retry, a validator fix may make the previous draft usable.
    # It remains untrusted until the complete input fingerprint and every current
    # validation rule pass. Never repair it, borrow it after an input change, or
    # treat its presence as permission to submit a video by itself.
    if (isinstance(previous_diagnostic, dict)
            and previous_diagnostic.get('version') == 1
            and previous_diagnostic.get('source_fingerprint') == fingerprint
            and previous_diagnostic.get('input_mode') == input_mode
            and previous_diagnostic.get('draft_truncated') is False
            and isinstance(previous_diagnostic.get('draft_prompt'), str)
            and 0 < len(previous_diagnostic['draft_prompt']) <= 16000):
        try:
            prepared = _validate(previous_diagnostic['draft_prompt'], duration, **validation)
        except ValueError:
            pass
        else:
            shot['_h3_diagnostic_reused'] = True
            return prepared, fingerprint
    generic = str(shot.get("video_prompt") or "").strip()
    if not generic:
        raise ValueError("通用视频提示词为空，无法转换为 H3 提示词")
    payload = {
        "prompt_language": H3_PROMPT_LANGUAGE,
        "input_mode": input_mode,
        "h3_prompt_format": _FORMATS[input_mode],
        "requested_duration_seconds": duration,
        "allow_cuts": allow_cuts,
        "model_neutral_video_prompt": generic,
        "approved_visible_texts": approved_visible_texts(shot.get("motion_plan")),
        "reference_audio_supplied": reference_audio,
        "reference_audio_requires_lipsync": bool(reference_audio and lipsync),
        "core_storyboard_image_attached": True,
        "core_storyboard_image_sha256": image_digest,
        "attached_image_count": len(images),
        "reference_assets": assets + (
            [{"label": "<Audio 1>", "role": "TTS timing and silent performance reference"}]
            if reference_audio else []
        ),
        "confirmed_character_inputs": shot.get('_confirmed_character_inputs'),
    }
    raw = generate_gemini_text(
        system_prompt=_system_for_mode(input_mode, duration, allow_cuts=allow_cuts),
        user_prompt=json.dumps(payload, ensure_ascii=False),
        temperature=0.1,
        max_output_tokens=7000,
        response_mime_type="application/json",
        json_root="object",
        image_data=images if len(images) > 1 else images[0],
    )
    result = None
    try:
        result = parse_json_response(raw)
        for _ in range(2):
            if not isinstance(result, str):
                break
            result = parse_json_response(result)
        if not isinstance(result, dict) or not isinstance(result.get("h3_prompt"), str):
            raise ValueError("H3 提示词 Agent 未返回有效 JSON 对象")
        return _validate(result["h3_prompt"], duration, **validation), fingerprint
    except ValueError as exc:
        raise H3PromptValidationError(str(exc), source_fingerprint=fingerprint,
            input_mode=input_mode, raw_response=raw,
            draft_prompt=result.get('h3_prompt', '') if isinstance(result, dict) else '') from exc
