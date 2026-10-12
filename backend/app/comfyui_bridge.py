"""Configurable ComfyUI bridge used by the standalone workbench.

The bridge deliberately stores connections separately from workflow profiles:
moving a workflow to another ComfyUI server must not invalidate its node map.
Only API-format workflows are executed; browser canvas JSON contains UI state but
not the complete prompt graph expected by ComfyUI's ``/prompt`` endpoint.
"""
from __future__ import annotations

import copy
import functools
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import requests
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .auth import require_user
from .video_render_options import VideoRenderOptions
from .comfyui_guard import (ENV_NAME as GUARD_ENV_NAME, GuardSubmissionError,
                           GuardWaitingStopped, recover_guard_submission, submit_prompt,
                           wait_for_guard_release)
from .comfyui_guard import stop_submission


router = APIRouter(prefix="/api/comfyui")
ROOT = Path(__file__).resolve().parents[2] / "workspace" / "comfyui"
LOCK = threading.RLock()
CONNECTION_CONTEXT = threading.local()


DEFAULT_CONNECTION = {
    "mode": "external",
    "name": "本机 ComfyUI",
    "base_url": "http://127.0.0.1:8188",
    "websocket_url": "",
    "prompt_path": "/prompt",
    "history_path": "/history/{prompt_id}",
    "upload_path": "/upload/image",
    "view_path": "/view",
    "interrupt_path": "/interrupt",
}


class ConnectionRequest(BaseModel):
    mode: Literal["external", "managed"] = "external"
    name: str = Field(default="本机 ComfyUI", min_length=1, max_length=80)
    base_url: str = Field(default="http://127.0.0.1:8188", min_length=1, max_length=2048)
    websocket_url: str = Field(default="", max_length=2048)
    prompt_path: str = Field(default="/prompt", min_length=1, max_length=512)
    history_path: str = Field(default="/history/{prompt_id}", min_length=1, max_length=512)
    upload_path: str = Field(default="/upload/image", min_length=1, max_length=512)
    view_path: str = Field(default="/view", min_length=1, max_length=512)
    interrupt_path: str = Field(default="/interrupt", min_length=1, max_length=512)


class Binding(BaseModel):
    node_id: str = Field(default="", max_length=80)
    input_name: str = Field(default="", max_length=120)


class WorkflowMappings(BaseModel):
    prompt: Binding = Field(default_factory=Binding)
    image: Binding = Field(default_factory=Binding)
    last_image: Binding = Field(default_factory=Binding)
    reference_images: list[Binding] = Field(default_factory=list, max_length=8)
    audio: Binding = Field(default_factory=Binding)
    duration: Binding = Field(default_factory=Binding)
    width: Binding = Field(default_factory=Binding)
    height: Binding = Field(default_factory=Binding)
    seed: Binding = Field(default_factory=Binding)
    steps: Binding = Field(default_factory=Binding)
    output_node_id: str = Field(default="", max_length=80)
    duration_mode: Literal["seconds", "frames"] = "frames"
    fps: float = Field(default=24, gt=0, le=240)
    frame_divisor: int = Field(default=1, ge=1, le=1000)
    frame_remainder: int = Field(default=0, ge=0, le=999)


class ProfileRequest(BaseModel):
    id: str = Field(default="", max_length=80)
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["image", "video", "upscale", "custom"] = "custom"
    video_input_mode: Literal["single_first", "first_last", "composition_reference"] = "single_first"
    h3_opening_guide: bool = False
    engine: Literal["external", "managed"] = "external"
    workflow: dict[str, Any]
    mappings: WorkflowMappings = Field(default_factory=WorkflowMappings)
    resolution_preset: Literal["480p", "720p", "1080p", "custom"] = "720p"
    default_width: int = Field(default=1280, ge=32, le=8192)
    default_height: int = Field(default=720, ge=32, le=8192)
    default_seed: int = Field(default=-1, ge=-1, le=2**63 - 1)


def _user_root(user_id: int) -> Path:
    result = ROOT / str(user_id)
    result.mkdir(parents=True, exist_ok=True)
    return result


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return copy.deepcopy(fallback)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _saved_connection(user_id: int) -> dict[str, Any]:
    return {**DEFAULT_CONNECTION, **_read_json(_user_root(user_id) / "connection.json", {})}


def _connection(user_id: int) -> dict[str, Any]:
    captured = getattr(CONNECTION_CONTEXT, "value", None)
    if captured and captured[0] == user_id:
        return captured[1]
    value = _saved_connection(user_id)
    if value.get("mode") == "managed":
        from .managed_comfyui import status
        state = status()
        if state["state"] != "ready":
            raise RuntimeError("内置视频引擎尚未启动；点击生成会自动启动，也可在 ComfyUI 工作台手动启动。")
        return {**DEFAULT_CONNECTION, "mode": "managed", "name": "OCV 内置 ComfyUI", "base_url": state["base_url"]}
    return value


def _managed_execution(function):
    @functools.wraps(function)
    def wrapped(user_id, *args, **kwargs):
        output = kwargs.get('output_path')
        receipt_present = bool(output and (Path(output).parent / 'comfyui_submission' / 'receipt.json').exists())
        if kwargs.get('existing_prompt_id') or receipt_present:
            profile = {'engine': kwargs.get('execution_engine') or 'external'}
        else:
            profile = (args[1] if len(args) > 1 and isinstance(args[1], dict) else
                       video_profile(user_id, args[0] if args else kwargs["profile_id"]))
        previous = getattr(CONNECTION_CONTEXT, "value", None)
        if profile.get("engine", "external") != "managed":
            CONNECTION_CONTEXT.value = (user_id, {**_saved_connection(user_id), "mode": "external"})
            try:
                return function(user_id, *args, **kwargs)
            finally:
                CONNECTION_CONTEXT.value = previous
        from .managed_comfyui import execution_lease
        if callable(kwargs.get("progress")):
            kwargs["progress"]("正在检查并启动 OCV 内置视频引擎")
        with execution_lease(required_models=profile.get("required_models"),
                             required_nodes={n['class_type'] for n in profile.get('workflow', {}).values()}) as url:
            CONNECTION_CONTEXT.value = (user_id, {**DEFAULT_CONNECTION, "mode": "managed", "base_url": url})
            try:
                return function(user_id, *args, **kwargs)
            finally:
                CONNECTION_CONTEXT.value = previous
    return wrapped


def _validate_connection(value: dict[str, Any]) -> dict[str, Any]:
    parsed = urlparse(str(value.get("base_url") or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ComfyUI 地址必须是完整的 http:// 或 https:// 地址")
    if parsed.username or parsed.password:
        raise ValueError("请勿把用户名或密码写进 ComfyUI 地址")
    result = dict(value)
    result["base_url"] = str(value["base_url"]).rstrip("/")
    for key in ("prompt_path", "history_path", "upload_path", "view_path", "interrupt_path"):
        path = str(value.get(key) or "").strip()
        if not path.startswith("/"):
            raise ValueError(f"{key} 必须以 / 开头")
        result[key] = path
    websocket = str(value.get("websocket_url") or "").strip()
    if websocket and urlparse(websocket).scheme not in {"ws", "wss"}:
        raise ValueError("WebSocket 地址必须以 ws:// 或 wss:// 开头")
    return result


def _url(connection: dict[str, Any], path: str) -> str:
    return urljoin(connection["base_url"] + "/", path.lstrip("/"))


def _profiles(user_id: int) -> list[dict[str, Any]]:
    values = _read_json(_user_root(user_id) / "profiles.json", [])
    values = values if isinstance(values, list) else []
    from .managed_comfyui import builtin_profiles, maintenance_enabled
    builtins = builtin_profiles()
    managed = []
    for builtin in builtins:
        saved = next((item for item in values if item.get("id") == builtin["id"]), None)
        current = {**saved, "engine": "managed", "managed_builtin": True} if saved and maintenance_enabled() else builtin
        managed.append(current)
    builtin_ids = {item["id"] for item in builtins}
    return [*managed, *[item for item in values if item.get("id") not in builtin_ids]]


def video_profile(user_id: int, profile_id: str) -> dict[str, Any]:
    value = next((item for item in _profiles(user_id) if item.get("id") == profile_id), None)
    if not value:
        raise ValueError("所选 ComfyUI 工作流预设不存在，请回到画面编排重新选择")
    if value.get("kind") != "video":
        raise ValueError("所选 ComfyUI 预设不是跑视频工作流")
    _api_graph(value.get("workflow") or {})
    return expand_h3_reference_capacity(value)


def expand_h3_reference_capacity(profile: dict[str, Any]) -> dict[str, Any]:
    """Give recognized H3 composition workflows all nine image inputs in memory.

    Saved presets often expose only four extra references. Validate their
    existing wiring before adding independent loaders; submission still prunes
    every unused slot and never edits the saved preset.
    """
    if profile.get("video_input_mode") != "composition_reference":
        return profile
    graph = _api_graph(profile.get("workflow") or {})
    targets = [node for node in graph.values()
               if node.get("class_type") in {"MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"}]
    if len(targets) != 1 or targets[0].get("class_type") != "MiniMaxH3ReferenceToVideo":
        return profile
    mappings = WorkflowMappings.model_validate(profile.get("mappings") or {})
    count = len(mappings.reference_images)
    if count >= 8:
        return profile
    validate_video_inputs(profile, input_mode="composition_reference", reference_count=count, has_last=False)
    result = copy.deepcopy(profile)
    graph = _api_graph(result["workflow"])
    target = next(node for node in graph.values() if node.get("class_type") == "MiniMaxH3ReferenceToVideo")
    bindings = result["mappings"].setdefault("reference_images", [])
    for index in range(count, 8):
        node_id = f"ocv_h3_reference_{index + 1}"
        while node_id in graph:
            node_id += "_"
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": ""}}
        bindings.append({"node_id": node_id, "input_name": "image"})
        target["inputs"][f"ref_images.ref_image_{index + 1}"] = [node_id, 0]
    return result


def _api_graph(workflow: dict[str, Any]) -> dict[str, Any]:
    candidate = workflow.get("prompt") if isinstance(workflow.get("prompt"), dict) else workflow
    if "nodes" in candidate and isinstance(candidate.get("nodes"), list):
        raise ValueError("这是 ComfyUI 画布工作流。请在 ComfyUI 开启开发者模式后导出“API 格式”工作流再导入。")
    if not candidate or not all(isinstance(value, dict) and value.get("class_type") for value in candidate.values()):
        raise ValueError("无法识别工作流；需要 ComfyUI API 格式的 JSON")
    return candidate


def _node_summary(graph: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for node_id, node in graph.items():
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        result.append({
            "id": str(node_id),
            "class_type": str(node.get("class_type") or ""),
            "title": str(meta.get("title") or node.get("class_type") or node_id),
            # Linked inputs remain selectable: mapping width/height directly on
            # a sampler intentionally replaces an upstream resolution link.
            "inputs": [str(key) for key in inputs],
        })
    return result


def _binding(graph: dict[str, Any], data: Binding, label: str, *, required: bool = False) -> tuple[dict[str, Any], str] | None:
    if not data.node_id and not data.input_name and not required:
        return None
    node = graph.get(str(data.node_id))
    if not isinstance(node, dict):
        raise ValueError(f"{label}映射的节点 {data.node_id or '（空）'} 不存在")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict) or data.input_name not in inputs:
        raise ValueError(f"{label}映射的输入 {data.input_name or '（空）'} 不存在")
    return inputs, data.input_name


def _profile_bindings(mappings: WorkflowMappings) -> list[tuple[str, Binding]]:
    return [
        ("提示词", mappings.prompt), ("首图 / Picture 1", mappings.image),
        ("末帧", mappings.last_image), ("参考音频", mappings.audio),
        ("时长", mappings.duration), ("宽度", mappings.width),
        ("高度", mappings.height), ("Seed", mappings.seed), ("采样步数", mappings.steps),
        *[(f"Picture {index + 2}", binding) for index, binding in enumerate(mappings.reference_images)],
    ]


def _disconnect_unused_image(graph: dict[str, Any], binding: Binding, protected_nodes: set[str]) -> None:
    """Only remove a plain loader whose consumers are known optional H3 inputs."""
    if not binding.node_id and not binding.input_name:
        return
    node_id = str(binding.node_id)
    node = graph.get(node_id, {})
    if node_id in protected_nodes or node.get("class_type") != "LoadImage" or binding.input_name != "image":
        raise ValueError(f"未使用图片槽 #{node_id} 无法安全断开；请移除预设中的旧图连接或使用独立 LoadImage.image")
    for consumer in graph.values():
        for field, value in (consumer.get("inputs") or {}).items():
            if not isinstance(value, list) or not value or str(value[0]) != node_id:
                continue
            optional = (
                consumer.get("class_type") == "MiniMaxH3ImageToVideo" and field == "last_frame"
            ) or (
                consumer.get("class_type") == "MiniMaxH3ReferenceToVideo"
                and re.fullmatch(r"ref_images\.ref_image_[0-8]", field)
            )
            if not optional:
                raise ValueError(f"未使用图片槽 #{node_id} 仍连接 {consumer.get('class_type')}.{field}，不能安全移除；请修改工作流")
    _disconnect_optional_reference(graph, node_id)


def _prune_unused_images(graph: dict[str, Any], mappings: WorkflowMappings, *, reference_count: int, has_last: bool) -> None:
    unused = ([] if has_last else [mappings.last_image]) + mappings.reference_images[reference_count:]
    unused_keys = {(binding.node_id, binding.input_name) for binding in unused}
    protected = {str(binding.node_id) for _, binding in _profile_bindings(mappings)
                 if binding.node_id and (binding.node_id, binding.input_name) not in unused_keys}
    if mappings.output_node_id:
        protected.add(str(mappings.output_node_id))
    for binding in unused:
        _disconnect_unused_image(graph, binding, protected)


def validate_video_inputs(profile: dict[str, Any], *, input_mode: str,
                          reference_count: int, has_last: bool) -> dict[str, Any]:
    """Validate declared media and graph wiring without contacting ComfyUI.

    reference_count counts Picture 2 onward; Picture 1 is always mappings.image.
    Unknown node classes remain configurable, but unused image routes must be
    demonstrably optional before we remove them from the submitted graph.
    """
    modes = {"single_first", "first_last", "composition_reference"}
    if input_mode not in modes:
        raise ValueError("视频输入模式无效")
    declared = str(profile.get("video_input_mode") or "single_first")
    if declared != input_mode:
        raise ValueError(f"本镜输入模式 {input_mode} 与预设模式 {declared} 不一致，请选择对应预设")
    if type(reference_count) is not int or not 0 <= reference_count <= 8:
        raise ValueError("额外参考图必须为 0–8 张（Picture 2–9）")
    if input_mode != "composition_reference" and reference_count:
        raise ValueError("首帧 / 首尾帧模式不接收额外角色参考，请显式选择多图参考模式")
    if bool(has_last) != (input_mode == "first_last"):
        raise ValueError("首尾帧模式必须提供末帧；其他模式不能附带末帧")
    mappings = WorkflowMappings.model_validate(profile.get("mappings") or {})
    if reference_count > len(mappings.reference_images):
        raise ValueError(f"预设只有 {len(mappings.reference_images)} 个额外参考槽，无法提交 {reference_count} 张参考图")
    graph = copy.deepcopy(_api_graph(profile.get("workflow") or {}))
    occupied: dict[tuple[str, str], str] = {}
    for label, binding in _profile_bindings(mappings):
        required = label in {"提示词", "首图 / Picture 1"} or (label == "末帧" and has_last) or label.startswith("Picture ")
        target = _binding(graph, binding, label, required=required)
        if target is not None:
            key = (str(binding.node_id), binding.input_name)
            if key in occupied:
                raise ValueError(f"{label} 与 {occupied[key]} 重复映射同一个输入槽，无法保持独立媒体绑定")
            occupied[key] = label
    if mappings.output_node_id and str(mappings.output_node_id) not in graph:
        raise ValueError(f"输出节点 {mappings.output_node_id} 不存在")
    _prune_unused_images(graph, mappings, reference_count=reference_count, has_last=has_last)
    h3_nodes = [node for node in graph.values() if node.get("class_type") in {
        "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"}]
    if h3_nodes:
        expected_class = "MiniMaxH3ReferenceToVideo" if input_mode == "composition_reference" else "MiniMaxH3ImageToVideo"
        if len(h3_nodes) != 1 or h3_nodes[0].get("class_type") != expected_class:
            raise ValueError(f"输入模式 {input_mode} 需要单个 {expected_class}，当前 H3 节点类型冲突")
        inputs = h3_nodes[0].get("inputs") or {}
        if input_mode == "composition_reference":
            expected = {f"ref_images.ref_image_{index}": binding for index, binding in enumerate(
                [mappings.image, *mappings.reference_images[:reference_count]])}
            actual_fields = {key for key in inputs if key.startswith("ref_images")}
            if actual_fields != set(expected) or "first_frame" in inputs or "last_frame" in inputs:
                raise ValueError("H3 多图参考槽与本镜 Picture 顺序不一致，或仍包含未声明的旧图片输入")
        else:
            expected = {"first_frame": mappings.image}
            if has_last:
                expected["last_frame"] = mappings.last_image
            elif inputs.get("last_frame") is not None:
                raise ValueError("单首帧工作流仍包含旧末帧，请配置末帧映射以安全断开，或移除旧连接")
            if any(key.startswith("ref_images") for key in inputs):
                raise ValueError("严格首帧工作流不能混用 Ref2VA 参考输入")
        for field, binding in expected.items():
            source = graph.get(str(binding.node_id), {})
            if (source.get("class_type") != "LoadImage" or binding.input_name != "image"
                    or inputs.get(field) != [str(binding.node_id), 0]):
                raise ValueError(f"H3 {field} 必须连接所声明的独立 LoadImage.image；当前映射不能证明图片按序实际送入")
    return {"input_mode": input_mode, "reference_capacity": len(mappings.reference_images),
            "image_count": 1 + reference_count + int(bool(has_last)), "has_last": bool(has_last)}


def _duration_value(seconds: float, mappings: WorkflowMappings) -> float | int:
    if mappings.duration_mode == "seconds":
        return round(seconds, 3)
    frames = max(1, math.ceil(seconds * mappings.fps))
    divisor = max(1, mappings.frame_divisor)
    remainder = mappings.frame_remainder % divisor
    frames += (remainder - frames) % divisor
    return frames


VIDEO_RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
}


def _video_dimensions(profile: dict[str, Any], ratio: str) -> tuple[int, int]:
    """Resolve a profile preset and orient it for the current OCV project."""
    preset = str(profile.get("resolution_preset") or "").strip().lower()
    base_width, base_height = VIDEO_RESOLUTION_PRESETS.get(
        preset,
        (
            int(profile.get("default_width") or 1280),
            int(profile.get("default_height") or 720),
        ),
    )
    landscape = ratio != "9:16"
    return (
        (max(base_width, base_height), min(base_width, base_height))
        if landscape
        else (min(base_width, base_height), max(base_width, base_height))
    )


def _available_resources() -> dict[str, float | None]:
    """Read free capacity without contacting ComfyUI or allocating GPU memory."""
    result: dict[str, float | None] = {"vram_free_gb": None, "ram_available_gb": None}
    try:
        import psutil
        result["ram_available_gb"] = round(psutil.virtual_memory().available / 1024**3, 1)
    except (ImportError, OSError):
        pass
    try:
        probe = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        free_mib = float(probe.stdout.splitlines()[0].strip())
        result["vram_free_gb"] = round(free_mib / 1024, 1)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return result


def _resource_estimate(profile: dict[str, Any], ratio: str, resolution: str, seconds: float,
                       available: dict[str, float | None], dimensions: tuple[int, int] | None = None) -> dict[str, Any]:
    """Conservative H3 planning heuristic, not a measured peak or admission guarantee."""
    chosen = dict(profile)
    if resolution in VIDEO_RESOLUTION_PRESETS:
        chosen["resolution_preset"] = resolution
    width, height = dimensions or _video_dimensions(chosen, ratio)
    mappings = WorkflowMappings.model_validate(profile.get("mappings") or {})
    frames = max(1, math.ceil(seconds * mappings.fps))
    pixels = width * height / (1280 * 720)
    workload = pixels * math.sqrt(frames / 240)
    # 1080p / ~13 s must remain high-risk on a 24 GB card: this class of
    # workflow has already exhausted such a device in normal OCV use.
    vram_low = round(9 + 4.5 * workload, 1)
    vram_high = round(11 + 7 * workload, 1)
    raw_frames_gb = width * height * frames * 4 / 1024**3
    ram_low = round(5 + raw_frames_gb * 2, 1)
    ram_high = round(8 + raw_frames_gb * 5, 1)
    vram_free, ram_free = available.get("vram_free_gb"), available.get("ram_available_gb")
    risks = []
    if vram_free is not None:
        risks.append("high" if vram_free < vram_low + 2 else "caution" if vram_free < vram_high + 2 else "low")
    if ram_free is not None:
        risks.append("high" if ram_free < ram_low + 4 else "caution" if ram_free < ram_high + 4 else "low")
    risk = max(risks, key={"low": 0, "caution": 1, "high": 2}.get) if risks else "unknown"
    return {"width": width, "height": height, "frames": frames, "fps": mappings.fps,
            "vram_estimate_gb": [vram_low, vram_high], "ram_estimate_gb": [ram_low, ram_high],
            **available, "risk": risk, "heuristic": True}


def _disconnect_optional_reference(graph: dict[str, Any], node_id: str) -> None:
    """Remove an unused loader and every graph input wired to its output."""
    node_id = str(node_id or "")
    if not node_id:
        return
    for node in graph.values():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for key, value in list(inputs.items()):
            if isinstance(value, list) and value and str(value[0]) == node_id:
                inputs.pop(key, None)
    graph.pop(node_id, None)


def _optional_audio_input(node: dict[str, Any], field: str) -> bool:
    if node.get("class_type") == "MiniMaxH3ReferenceToVideo" and field in {
        f"{group}.{prefix}_{index}"
        for group, prefix in (("ref_audios", "ref_audio"), ("ref_video_audios", "ref_video_audio"))
        for index in range(3)
    }:
        return True
    return field == "audio" and node.get("class_type") in {
        "MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo", "VHS_VideoCombine",
    }


def _audio_consumers(graph: dict[str, Any], node_id: str) -> list[tuple[dict[str, Any], str]]:
    return [(node, field) for node in graph.values() for field, value in (node.get("inputs") or {}).items()
            if isinstance(value, list) and value and str(value[0]) == node_id]


def _patch_audio_reference(graph: dict[str, Any], mappings: WorkflowMappings, audio_name: str) -> None:
    """Map filenames to loaders; disconnect optional sockets without deleting their owner."""
    mapped = mappings.audio
    target = _binding(graph, mapped, "参考音频")
    if target is None:
        return
    node_id = str(mapped.node_id)
    node = graph[node_id]
    value = target[0][target[1]]
    optional_socket = _optional_audio_input(node, mapped.input_name)
    protected = {str(binding.node_id) for label, binding in _profile_bindings(mappings)
                 if label != "参考音频" and binding.node_id}
    if mappings.output_node_id:
        protected.add(str(mappings.output_node_id))
    if audio_name:
        source_id = node_id if node.get("class_type") == "LoadAudio" and mapped.input_name == "audio" else ""
        if optional_socket or isinstance(value, list):
            source_id = str(value[0]) if isinstance(value, list) and len(value) == 2 else ""
            source = graph.get(source_id, {})
            if (not optional_socket or not source_id or value[1] != 0
                    or source.get("class_type") != "LoadAudio" or "audio" not in (source.get("inputs") or {})):
                raise ValueError("参考音频连接必须来自独立 LoadAudio.audio，请将映射指向可上传音频的加载节点")
            actual = Binding(node_id=source_id, input_name="audio")
            if any(label != "参考音频" and binding.node_id == source_id and binding.input_name == "audio"
                   for label, binding in _profile_bindings(mappings)):
                raise ValueError("参考音频来源与其他映射重复，无法独立替换上传文件")
            target = _binding(graph, actual, "参考音频来源", required=True)
        target[0][target[1]] = audio_name
        # Companion audio without its matching video is ignored by H3. Move
        # only this mapped loader to a standalone socket; preserve paired media.
        if source_id:
            for consumer in graph.values():
                if consumer.get("class_type") != "MiniMaxH3ReferenceToVideo":
                    continue
                inputs = consumer.get("inputs") or {}
                for field, link in list(inputs.items()):
                    if not field.startswith("ref_video_audios.ref_video_audio_"):
                        continue
                    index = field.rsplit("_", 1)[-1]
                    if link == [source_id, 0] and f"ref_videos.ref_video_{index}" not in inputs:
                        free = next((i for i in range(3) if f"ref_audios.ref_audio_{i}" not in inputs), None)
                        if free is None:
                            raise ValueError("H3 独立音频参考端口已满，请检查工作流音频连接")
                        inputs[f"ref_audios.ref_audio_{free}"] = inputs.pop(field)
        return
    if optional_socket:
        # A binding may point at H3.audio rather than LoadAudio.audio. Only the
        # selected input is optional; H3 and its video outputs must survive.
        target[0].pop(target[1])
        source_id = str(value[0]) if isinstance(value, list) and value else ""
        source = graph.get(source_id, {})
        if (source.get("class_type") == "LoadAudio" and source_id not in protected
                and not _audio_consumers(graph, source_id)):
            graph.pop(source_id)
        return
    if (node.get("class_type") != "LoadAudio" or mapped.input_name != "audio"
            or node_id in protected
            or any(not _optional_audio_input(consumer, field) for consumer, field in _audio_consumers(graph, node_id))):
        raise ValueError("未提供参考音频，当前映射无法安全断开；请使用独立 LoadAudio.audio 或已知可选音频输入")
    _disconnect_optional_reference(graph, node_id)


def _patch_graph(graph: dict[str, Any], mappings: WorkflowMappings, *, prompt: str, duration: float, width: int, height: int, seed: int, image_name: str = "", audio_name: str = "", last_image_name: str = "", reference_image_names: list[str] | None = None) -> dict[str, Any]:
    graph = copy.deepcopy(graph)
    reference_image_names = list(reference_image_names or [])
    if len(reference_image_names) > len(mappings.reference_images):
        raise ValueError("额外参考图片数量超过预设槽位，未提交工作流")
    _prune_unused_images(graph, mappings, reference_count=len(reference_image_names), has_last=bool(last_image_name))
    _patch_audio_reference(graph, mappings, audio_name)
    values = (
        ("提示词", mappings.prompt, prompt),
        ("参考图", mappings.image, image_name),
        ("时长", mappings.duration, _duration_value(duration, mappings)),
        ("宽度", mappings.width, width),
        ("高度", mappings.height, height),
        ("Seed", mappings.seed, seed),
    )
    for label, mapped, value in values:
        target = _binding(graph, mapped, label, required=label == "提示词")
        if target is not None:
            if label == "参考图" and not value:
                raise ValueError(f"当前预设需要{label}，请先提供")
            target[0][target[1]] = value
    media = ([("末帧", mappings.last_image, last_image_name)] if last_image_name else [])
    media += [(f"Picture {index + 2}", mappings.reference_images[index], name)
              for index, name in enumerate(reference_image_names)]
    for label, binding, name in media:
        target = _binding(graph, binding, label, required=True)
        if not name:
            raise ValueError(f"{label} 上传结果为空，未提交工作流")
        target[0][target[1]] = name
    if mappings.output_node_id and str(mappings.output_node_id) not in graph:
        raise ValueError(f"输出节点 {mappings.output_node_id} 不存在")
    return graph


VIDEO_OUTPUT_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi", ".mpeg", ".mpg", ".ogv"}


def _patch_voice_references(graph, mappings, names):
    """Explicit Ref2VA voice-timbre inputs; never reinterpret a narration track."""
    if not names:
        return graph
    if len(names) > 3 or any(not isinstance(name, str) or not name for name in names):
        raise ValueError('H3 对话实验最多采用3个明确的音色参考')
    target = graph.get(mappings.prompt.node_id, {})
    if target.get('class_type') != 'MiniMaxH3ReferenceToVideo':
        raise ValueError('角色音色参考只支持 Ref2VA，不能用首尾帧预设假装锁定声音')
    inputs = target.get('inputs', {})
    if any(key == 'audio' or key.startswith(('ref_audios.', 'ref_video_audios.')) for key in inputs):
        raise ValueError('预设已有音频输入，请使用不含旧声音的明确对话预设')
    for index, name in enumerate(names):
        identity = f'ocv_dialogue_voice_{index}'
        if identity in graph:
            raise ValueError('对话参考音频节点标识冲突')
        graph[identity] = {'class_type': 'LoadAudio', 'inputs': {'audio': name}}
        inputs[f'ref_audios.ref_audio_{index}'] = [identity, 0]
    return graph


def _patch_opening_guide(graph, mappings, *, enabled=False):
    """Anchor the approved opening without dropping Ref2VA identities/timbres.

    Opt-in at profile level until the installed native Guide is verified. This
    changes conditioning only, never replaces the video or its original audio.
    """
    if not enabled:
        return graph
    source_id = str(mappings.prompt.node_id)
    source = graph.get(source_id, {})
    inputs = source.get('inputs', {})
    if source.get('class_type') != 'MiniMaxH3ReferenceToVideo':
        raise ValueError('开场图约束仅用于 H3 多图参考预设；首尾帧预设已有自己的开场约束')
    image = inputs.get('ref_images.ref_image_0')
    vae = inputs.get('vae')
    if (image != [str(mappings.image.node_id), 0] or not isinstance(vae, list)
            or len(vae) != 2):
        raise ValueError('开场图约束缺少已验证的核心图或视频 VAE 连接')
    identity = 'ocv_core_opening_guide'
    if identity in graph or any(n.get('class_type') == 'MiniMaxH3AddGuide' for n in graph.values()):
        raise ValueError('预设已包含画面 Guide；请保留原有约束，不要重复启用自动开场图约束')
    consumers = [(node, field) for node in graph.values()
                 for field, value in node.get('inputs', {}).items()
                 if value == [source_id, 0] and field in {'positive', 'conditioning'}]
    if not consumers:
        raise ValueError('无法确认 H3 正向条件的采样连接，未提交开场图约束')
    graph[identity] = {'class_type': 'MiniMaxH3AddGuide', 'inputs': {
        'positive': [source_id, 0], 'latent': [source_id, 1],
        'image': image.copy(), 'vae': vae.copy(), 'frame_idx': 0}}
    for node, field in consumers:
        node['inputs'][field] = [identity, 0]
    return graph


def _require_opening_guide_support(connection):
    try:
        response = requests.get(_url(connection, '/object_info/MiniMaxH3AddGuide'), timeout=8)
        response.raise_for_status()
        supported = 'MiniMaxH3AddGuide' in response.json()
    except (requests.RequestException, ValueError) as exc:
        raise ValueError('无法确认开场图约束支持；请检查 ComfyUI 连接，或在视频预设中关闭开场图约束') from exc
    if not supported:
        raise ValueError('当前 ComfyUI 没有原生 H3 Guide；请升级 H3 节点，或在视频预设中关闭开场图约束继续制作')


def _patch_dialogue_audio_guide(graph, mappings, audio_name):
    if not audio_name:return graph
    source_id=str(mappings.prompt.node_id); source=graph.get(source_id,{})
    inputs=source.get('inputs',{});audio_vae=inputs.get('audio_vae')
    if source.get('class_type')!='MiniMaxH3ReferenceToVideo' or not isinstance(audio_vae,list) or len(audio_vae)!=2:
        raise ValueError('精确对白口型约束需要H3多参预设与Audio VAE连接')
    guide_id='ocv_dialogue_audio_guide';loader_id='ocv_dialogue_audio'
    if guide_id in graph or loader_id in graph:raise ValueError('对白音频Guide重复，未提交')
    positive_id='ocv_core_opening_guide' if 'ocv_core_opening_guide' in graph else source_id
    consumers=[(node,field) for node in graph.values() for field,value in node.get('inputs',{}).items()
               if value==[positive_id,0] and field in {'positive','conditioning'}]
    if not consumers:raise ValueError('无法确认对白音频约束的正向采样连接')
    graph[loader_id]={'class_type':'LoadAudio','inputs':{'audio':audio_name}}
    graph[guide_id]={'class_type':'MiniMaxH3AddGuide','inputs':{
        'positive':[positive_id,0],'latent':[source_id,1],'frame_idx':0,
        'audio_vae':audio_vae.copy(),'audio':[loader_id,0]}}
    for node,field in consumers:node['inputs'][field]=[guide_id,0]
    return graph


def _require_dialogue_audio_guide_support(connection):
    response=requests.get(_url(connection,'/object_info/MiniMaxH3AddGuide'),timeout=8)
    response.raise_for_status()
    info=response.json().get('MiniMaxH3AddGuide',{}).get('input',{})
    fields={**info.get('required',{}),**info.get('optional',{})}
    if not {'audio','audio_vae'}.issubset(fields):
        raise ValueError('当前H3 Guide不支持对白音频，请升级官方Guide节点后重试；未提交视频')


def _find_output_file(history: dict[str, Any], output_node_id: str, *, video_only: bool = False) -> dict[str, str] | None:
    outputs = history.get("outputs") if isinstance(history, dict) else None
    if not isinstance(outputs, dict):
        return None
    selected = outputs.get(str(output_node_id)) if output_node_id else None
    groups = [selected] if isinstance(selected, dict) else list(outputs.values())
    for group in groups:
        if not isinstance(group, dict):
            continue
        for key in (("videos", "gifs", "images") if video_only else ("gifs", "videos", "images", "audio")):
            for item in group.get(key) or []:
                if isinstance(item, dict) and item.get("filename"):
                    # VHS reports video containers in `gifs`; other output
                    # nodes may use `images`. The filename must still be a
                    # video container, even if the result group says `videos`.
                    if video_only and Path(str(item["filename"])).suffix.lower() not in VIDEO_OUTPUT_SUFFIXES:
                        continue
                    return {"filename": str(item["filename"]), "subfolder": str(item.get("subfolder") or ""), "type": str(item.get("type") or "output")}
    return None


def _history_payload(connection: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
    """Read one history item; a busy local server timeout is not task failure.

    Some large video workflows block ComfyUI's HTTP handler while loading or
    offloading models. Once ``prompt_id`` is known, retrying the status GET is
    safe while resubmitting the workflow is not.
    """
    if connection.get("mode") == "managed":
        from .managed_comfyui import status
        state = status()
        if state["state"] != "ready":
            raise RuntimeError(state.get("error") or "内置引擎已停止，当前任务无法继续；请检查引擎日志后重新生成。")
    history_path = connection["history_path"].replace("{prompt_id}", prompt_id)
    try:
        response = requests.get(_url(connection, history_path), timeout=(5, 30))
    except (requests.Timeout, requests.ConnectionError):
        return None
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _queued_prompt_ids(connection: dict[str, Any]) -> tuple[set[str], set[str]] | None:
    """Return running and pending prompt ids, or ``None`` when status is unknown."""
    try:
        response = requests.get(_url(connection, "/queue"), timeout=(5, 15))
        response.raise_for_status()
        payload = response.json()
    except (requests.Timeout, requests.ConnectionError, requests.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return set(), set()

    def identities(items: Any) -> set[str]:
        result: set[str] = set()
        for item in items or []:
            if isinstance(item, (list, tuple)) and len(item) > 1:
                result.add(str(item[1]))
            elif isinstance(item, dict):
                value = item.get("prompt_id") or item.get("promptId")
                if value:
                    result.add(str(value))
        return result

    return identities(payload.get("queue_running")), identities(payload.get("queue_pending"))


def _history_failure(history: dict[str, Any]) -> str:
    """Only explicit terminal events end polling; incomplete is not failure."""
    status = history.get('status') or {}
    if not isinstance(status, dict):
        return ''
    for item in reversed(status.get('messages') or []):
        if not isinstance(item, (list, tuple)) or not item:
            continue
        if item[0] == 'execution_interrupted':
            detail = item[1] if len(item) > 1 and isinstance(item[1], dict) else {}
            node = detail.get('node_type') or detail.get('node_id')
            return 'ComfyUI 任务已中断' + (f'（节点：{node}）' if node else '') + '，不会再返回结果，请重新生成。'
        if item[0] == 'execution_error':
            return 'ComfyUI 执行失败：' + str(item[1] if len(item) > 1 else '服务端未提供详情')
    if str(status.get('status_str') or '').lower() in {'error', 'failed', 'interrupted', 'cancelled', 'canceled'}:
        return 'ComfyUI 任务已失败或中断，服务端未提供详细原因，请查看 ComfyUI 日志后重新生成。'
    return ''


def video_task_state(user_id: int, prompt_id: str, profile_id: str = "") -> str:
    """Inspect a persisted local task without ever submitting a replacement.

    ``missing`` is returned only after the task is absent from history and both
    queue buckets, followed by a second history read. This matters after a full
    ComfyUI restart: its in-memory queue/history can disappear while OCV still
    safely remembers the old prompt id.
    """
    prompt_id = str(prompt_id or "").strip()
    if not prompt_id:
        return "missing"
    if profile_id:
        profile = video_profile(user_id, profile_id)
        if profile.get("engine", "external") == "managed":
            from .managed_comfyui import status
            state = status()
            if state["state"] != "ready":
                return "missing"
            connection = {**DEFAULT_CONNECTION, "mode": "managed", "base_url": state["base_url"]}
        else:
            connection = {**_saved_connection(user_id), "mode": "external"}
    else:
        connection = _connection(user_id)

    def history_state(payload: dict[str, Any] | None) -> str:
        if payload is None:
            return "unknown"
        history = payload.get(prompt_id) if isinstance(payload, dict) else None
        if not isinstance(history, dict):
            return "absent"
        if _history_failure(history):
            return "failed"
        if _find_output_file(history, "", video_only=True):
            return "completed"
        return "history"

    first = history_state(_history_payload(connection, prompt_id))
    if first != "absent":
        return first
    queued = _queued_prompt_ids(connection)
    if queued is None:
        return "unknown"
    running, pending = queued
    if prompt_id in running:
        return "running"
    if prompt_id in pending:
        return "pending"
    # Close the small race where a task leaves the queue between the two GETs.
    second = history_state(_history_payload(connection, prompt_id))
    return "missing" if second == "absent" else second


def _job_path(user_id: int, job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise HTTPException(404, "任务不存在")
    return _user_root(user_id) / "jobs" / job_id


def _save_job(path: Path, value: dict[str, Any]) -> None:
    with LOCK:
        _write_json(path / "record.json", value)


def _upload_reference(connection: dict[str, Any], source: Path) -> str:
    # Different projects (or ordered pictures) can share a local basename.
    # Each submission owns its uploaded bytes until ComfyUI consumes them.
    upload_name = f"ocv-{uuid.uuid4().hex}{source.suffix.lower()}"
    with source.open("rb") as stream:
        response = requests.post(
            _url(connection, connection["upload_path"]),
            files={"image": (upload_name, stream, mimetypes.guess_type(source.name)[0] or "application/octet-stream")},
            data={"type": "input", "overwrite": "false"}, timeout=120,
        )
    response.raise_for_status()
    payload = response.json()
    name = str(payload.get("name") or "")
    if not name:
        raise RuntimeError("ComfyUI 已接收图片，但没有返回文件名")
    subfolder = str(payload.get("subfolder") or "").strip("/\\")
    return f"{subfolder}/{name}" if subfolder else name


class ComfyUIStopped(RuntimeError):
    pass


@_managed_execution
def run_video_profile(user_id: int, profile_id: str, *, prompt: str, image_path: Path,
                      duration: float, ratio: str, output_path: Path, progress=None,
                      should_stop=None, seed: int = -1, existing_prompt_id: str = '',
                      existing_output_node_id: str = '',
                      on_submitted=None, audio_path: Path | None = None,
                      input_mode: str = 'single_first', last_image_path: Path | None = None,
                      reference_image_paths: list[Path] | tuple[Path, ...] | None = None,
                      voice_reference_paths: list[Path] | tuple[Path, ...] | None = None,
                      dialogue_audio_path: Path | None = None,
                      render_options: dict | None = None, resolution: str = '', dimensions: tuple[int, int] | None = None,
                      execution_engine: str = '') -> None:
    """Run one configured video preset synchronously for a storyboard worker."""
    connection = _connection(user_id)
    submission_dir = output_path.parent / "comfyui_submission"
    prompt_id = str(existing_prompt_id or '').strip()
    if not prompt_id or (submission_dir / "receipt.json").exists():
        def recovered_submitted(remote_id):
            if prompt_id and prompt_id != remote_id:
                raise GuardSubmissionError("原任务 ID 与 guard 提交凭据冲突，已停止恢复")
            if on_submitted:
                on_submitted(remote_id)

        try:
            recovered_id = recover_guard_submission(connection, submission_dir,
                                                    on_submitted=recovered_submitted, progress=progress,
                                                    should_stop=should_stop)
        except GuardWaitingStopped as exc:
            error=ComfyUIStopped(str(exc));error.terminal=getattr(exc,'terminal',False)
            raise error from exc
        prompt_id = recovered_id or prompt_id
    if prompt_id:
        # The server already owns the frozen graph and uploaded media. A later
        # edit or deletion of the local preset must not block history recovery.
        output_node_id = str(existing_output_node_id or '').strip()
        if progress:
            progress(f"继续查询原 ComfyUI 任务 {prompt_id}，不会重复提交")
    else:
        if should_stop and should_stop():
            raise ComfyUIStopped("尚未提交，已停止本地生成。")
        from .video_render_options import apply as apply_render_options
        profile = apply_render_options(video_profile(user_id, profile_id), render_options)
        if resolution:
            if resolution == 'custom':
                if not dimensions or len(dimensions) != 2 or any(type(value) is not int or value < 32 or value > 8192 for value in dimensions):
                    raise ValueError('请提供有效的自定义视频宽高（32–8192 像素）')
            elif resolution not in VIDEO_RESOLUTION_PRESETS:
                raise ValueError('不支持的视频分辨率')
            profile = {**profile, 'resolution_preset': resolution}
        mappings = WorkflowMappings.model_validate(profile.get("mappings") or {})
        output_node_id = mappings.output_node_id
        width, height = dimensions or _video_dimensions(profile, ratio)
        actual_seed = seed if seed >= 0 else uuid.uuid4().int % (2**63 - 1)
        references = list(reference_image_paths or [])
        voices = list(voice_reference_paths or [])
        if dialogue_audio_path is not None:
            if voices or audio_path is not None:raise ValueError('对白音频Guide不能混用音色参考或旁白输入')
            _require_dialogue_audio_guide_support(connection)
            _patch_dialogue_audio_guide(copy.deepcopy(_api_graph(profile['workflow'])),mappings,'preflight-dialogue.wav')
        if voices and audio_path is not None:
            raise ValueError('音色参考与整段旁白输入不能同时提交')
        if voices:
            from .h3_voice_reference import validate_voice_references
            validate_voice_references(voices)
            _patch_voice_references(copy.deepcopy(_api_graph(profile['workflow'])), mappings,
                                    [f'preflight-voice-{i}.wav' for i in range(len(voices))])
        validate_video_inputs(profile, input_mode=input_mode, reference_count=len(references),
                              has_last=last_image_path is not None)
        _patch_opening_guide(copy.deepcopy(_api_graph(profile['workflow'])), mappings,
                             enabled=profile.get('h3_opening_guide', False))
        if profile.get('h3_opening_guide', False):
            _require_opening_guide_support(connection)
        _patch_audio_reference(copy.deepcopy(_api_graph(profile["workflow"])), mappings,
                               "ocv-audio-preflight.wav" if audio_path else "")
        for source in [image_path, *([last_image_path] if last_image_path is not None else []),
                       *references, *voices, *([audio_path] if audio_path is not None else []),
                       *([dialogue_audio_path] if dialogue_audio_path is not None else [])]:
            if not Path(source).is_file():
                raise ValueError("本镜视频输入素材不存在，未上传或提交工作流")
        if progress:
            progress(f"正在按 {input_mode} 上传本镜 {1 + len(references) + int(last_image_path is not None)} 张图片")
        image_name = _upload_reference(connection, image_path)
        last_image_name = _upload_reference(connection, last_image_path) if last_image_path is not None else ""
        reference_image_names = [_upload_reference(connection, source) for source in references]
        audio_name = _upload_reference(connection, audio_path) if audio_path else ""
        graph = _patch_graph(_api_graph(profile["workflow"]), mappings, prompt=prompt,
                             duration=duration, width=width, height=height,
                             seed=actual_seed, image_name=image_name, audio_name=audio_name,
                             last_image_name=last_image_name, reference_image_names=reference_image_names)
        voice_names = [_upload_reference(connection, source) for source in voices]
        _patch_voice_references(graph, mappings, voice_names)
        _patch_opening_guide(graph, mappings, enabled=profile.get('h3_opening_guide', False))
        if dialogue_audio_path is not None:
            _patch_dialogue_audio_guide(graph,mappings,_upload_reference(connection,dialogue_audio_path))
        if should_stop and should_stop():
            raise ComfyUIStopped("图片已上传，但提交前已停止；尚未创建 ComfyUI 任务。")
        client_id = "ocv-video-" + uuid.uuid4().hex
        try:
            prompt_id = submit_prompt(connection, graph, client_id, submission_dir,
                                      on_submitted=on_submitted, progress=progress, should_stop=should_stop)
        except GuardWaitingStopped as exc:
            error=ComfyUIStopped(str(exc));error.terminal=getattr(exc,'terminal',False)
            raise error from exc
        if progress:
            progress(f"ComfyUI 已接收任务 {prompt_id}，等待服务端结果（可能排队或生成中）")
    deadline = time.monotonic() + 7200
    interrupted = False
    timeout_notified = False
    while time.monotonic() < deadline:
        if should_stop and should_stop():
            if (submission_dir / "receipt.json").exists() or os.environ.get(GUARD_ENV_NAME, "").strip():
                try:stop_submission(connection,submission_dir)
                except GuardWaitingStopped as exc:
                    error=ComfyUIStopped(str(exc));error.terminal=getattr(exc,'terminal',False)
                    raise error from exc
            if not interrupted:
                try:
                    requests.post(_url(connection, connection["interrupt_path"]), timeout=10).raise_for_status()
                finally:
                    interrupted = True
            raise ComfyUIStopped("已请求终止 ComfyUI 当前任务。")
        payload = _history_payload(connection, prompt_id)
        if payload is None:
            if progress and not timeout_notified:
                progress("ComfyUI 正忙，状态查询超过 30 秒；任务仍在服务端运行，OCV 将继续等待且不会重复提交")
            timeout_notified = True
            time.sleep(2)
            continue
        history = payload.get(prompt_id) if isinstance(payload, dict) else None
        if isinstance(history, dict):
            status = history.get("status") or {}
            failure = _history_failure(history)
            if failure:
                raise RuntimeError(failure)
            result = _find_output_file(history, output_node_id, video_only=True)
            if result:
                if progress:
                    progress("ComfyUI 已生成结果，正在下载视频")
                downloaded = requests.get(_url(connection, connection["view_path"]),
                                          params=result, timeout=300)
                downloaded.raise_for_status()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(downloaded.content)
                if not output_path.is_file() or output_path.stat().st_size <= 0:
                    raise RuntimeError("ComfyUI 返回了空的视频文件")
                if progress:
                    progress("ComfyUI 视频已下载到当前镜头")
                try:
                    wait_for_guard_release(submission_dir, prompt_id, progress=progress, should_stop=should_stop)
                except GuardWaitingStopped as exc:
                    raise ComfyUIStopped(str(exc)) from exc
                return
            if status.get("completed") is True or status.get("status_str") == "success":
                raise RuntimeError("ComfyUI 任务已完成，但所选输出中没有视频文件；请核对视频输出节点")
        time.sleep(2)
    raise TimeoutError("等待 ComfyUI 视频结果超时")


def _execute(user_id: int, job_id: str, profile: dict[str, Any], source: Path | None,
             audio_source: Path | None, values: dict[str, Any]) -> None:
    try:
        _execute_with_engine(user_id, job_id, profile, source, audio_source, values)
    except Exception as exc:
        path = _job_path(user_id, job_id)
        record = _read_json(path / "record.json", {})
        record.update(status="failed", message=str(exc))
        _save_job(path, record)


@_managed_execution
def _execute_with_engine(user_id: int, job_id: str, profile: dict[str, Any], source: Path | None,
             audio_source: Path | None, values: dict[str, Any]) -> None:
    path = _job_path(user_id, job_id)
    record = _read_json(path / "record.json", {})
    try:
        connection = _connection(user_id)
        submission_dir = path / "comfyui_submission"

        prompt_id = str(record.get("remote_prompt_id") or "")

        def submitted(remote_id):
            if prompt_id and prompt_id != remote_id:
                raise GuardSubmissionError("原任务 ID 与 guard 提交凭据冲突，已停止恢复")
            record.update(remote_prompt_id=remote_id, message="ComfyUI 已接收，正在生成", progress=15)
            _save_job(path, record)

        if not prompt_id or (submission_dir / "receipt.json").exists():
            prompt_id = recover_guard_submission(connection, submission_dir, on_submitted=submitted) or prompt_id
        output_node_id = str(record.get("output_node_id") or "")
        if not prompt_id:
            if profile.get("kind") == "video":
                validate_video_inputs(profile, input_mode=profile.get("video_input_mode") or "single_first",
                                      reference_count=0, has_last=False)
            mappings = WorkflowMappings.model_validate(profile.get("mappings") or {})
            output_node_id = mappings.output_node_id
            _patch_opening_guide(copy.deepcopy(_api_graph(profile['workflow'])), mappings,
                                 enabled=profile.get('h3_opening_guide', False))
            if profile.get('h3_opening_guide', False):
                _require_opening_guide_support(connection)
            _patch_audio_reference(copy.deepcopy(_api_graph(profile["workflow"])), mappings,
                                   "ocv-audio-preflight.wav" if audio_source else "")
            record.update(message="正在上传参考素材" if source or audio_source else "正在提交工作流",
                          progress=5, output_node_id=output_node_id)
            _save_job(path, record)
            image_name = _upload_reference(connection, source) if source else ""
            audio_name = _upload_reference(connection, audio_source) if audio_source else ""
            graph = _patch_graph(_api_graph(profile["workflow"]), mappings, image_name=image_name,
                                 audio_name=audio_name, **values)
            _patch_opening_guide(graph, mappings, enabled=profile.get('h3_opening_guide', False))
            prompt_id = submit_prompt(connection, graph, "ocv-" + uuid.uuid4().hex,
                                      submission_dir, on_submitted=submitted)
        record.update(remote_prompt_id=prompt_id, message="ComfyUI 已接收，正在生成", progress=15)
        _save_job(path, record)
        deadline = time.monotonic() + 7200
        timeout_notified = False
        while time.monotonic() < deadline:
            payload = _history_payload(connection, prompt_id)
            if payload is None:
                if not timeout_notified:
                    record.update(message="ComfyUI 正忙，状态查询超时；任务仍在运行，继续等待", progress=max(15, record.get("progress", 0)))
                    _save_job(path, record)
                timeout_notified = True
                time.sleep(2)
                continue
            history = payload.get(prompt_id) if isinstance(payload, dict) else None
            if isinstance(history, dict):
                failure = _history_failure(history)
                if failure:
                    raise RuntimeError(failure)
                output = _find_output_file(history, output_node_id, video_only=profile.get("kind") == "video")
                if output:
                    downloaded = requests.get(_url(connection, connection["view_path"]), params=output, timeout=300)
                    downloaded.raise_for_status()
                    suffix = Path(output["filename"]).suffix.lower() or ".bin"
                    destination = path / ("result" + suffix)
                    destination.write_bytes(downloaded.content)
                    record.update(status="completed", message="生成完成", progress=100, result_name=destination.name)
                    _save_job(path, record)
                    return
                if profile.get("kind") == "video" and (status.get("completed") is True or status.get("status_str") == "success"):
                    raise RuntimeError("ComfyUI 任务已完成，但所选输出中没有视频文件；请核对视频输出节点")
            time.sleep(2)
        raise TimeoutError("等待 ComfyUI 结果超时")
    except Exception as exc:
        record.update(status="failed", message=str(exc), progress=record.get("progress", 0))
        _save_job(path, record)


@router.get("")
def overview(request: Request) -> dict[str, Any]:
    user_id = int(require_user(request)["id"])
    connection = _saved_connection(user_id)
    profiles = _profiles(user_id)
    for value in profiles:
        if value.get('video_input_mode') == 'composition_reference':
            try:
                effective = expand_h3_reference_capacity(value)
                value['effective_reference_capacity'] = len((effective.get('mappings') or {}).get('reference_images') or [])
            except ValueError:
                value['effective_reference_capacity'] = len((value.get('mappings') or {}).get('reference_images') or [])
    from .managed_comfyui import maintenance_enabled
    return {"managed_maintenance": maintenance_enabled(), "connection": connection, "default_video": _read_json(_user_root(user_id) / 'creation-defaults.json', None), "profiles": [{**value, "workflow": None, "nodes": _node_summary(_api_graph(value["workflow"]))} for value in profiles]}


class CreationDefaults(VideoRenderOptions):
    video_generation_backend: Literal['api', 'comfyui'] = 'api'
    comfyui_profile_id: str = Field(default='', max_length=80)
    comfyui_h3_prompt_agent: bool = False
    comfyui_reference_audio: bool = False
    image_profile_id: str = Field(default='', max_length=80)
    image_resolution: Literal['1k', '2k', '4k'] = '1k'


@router.get('/creation-defaults')
def creation_defaults(request: Request):
    user_id = int(require_user(request)['id'])
    return {'defaults': _read_json(_user_root(user_id) / 'creation-defaults.json', None)}


@router.put('/creation-defaults')
def save_creation_defaults(data: CreationDefaults, request: Request):
    user_id = int(require_user(request)['id'])
    try:
        if data.video_generation_backend == 'comfyui':
            profile = video_profile(user_id, data.comfyui_profile_id)
            from .video_render_options import apply as apply_render_options
            apply_render_options(profile, data.model_dump())
            if data.comfyui_reference_audio:
                audio = profile.get('mappings', {}).get('audio', {})
                if data.comfyui_h3_prompt_agent or not audio.get('node_id') or not audio.get('input_name'):
                    raise ValueError('该工作流不能同时采用当前参考音频设置')
        elif data.comfyui_reference_audio:
            raise ValueError('视频 API 不使用 ComfyUI 参考音频')
        if data.image_profile_id:
            from .image_profiles import profile_snapshot
            profile_snapshot(data.image_profile_id, data.image_resolution)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    value = data.model_dump()
    path = _user_root(user_id) / 'creation-defaults.json'
    temporary = path.with_name('.defaults.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {'defaults': value}

@router.get("/resource-estimate")
def resource_estimate(request: Request, profile_id: str = Query(min_length=1, max_length=80),
                      ratio: Literal["16:9", "9:16"] = "16:9",
                      resolution: str = Query(default="", max_length=12),
                      seconds: float = Query(default=10, gt=0, le=120),
                      width: int | None = Query(default=None, ge=32, le=8192),
                      height: int | None = Query(default=None, ge=32, le=8192)) -> dict[str, Any]:
    user_id = int(require_user(request)["id"])
    if resolution and resolution not in {*VIDEO_RESOLUTION_PRESETS, 'custom'}:
        raise HTTPException(422, "分辨率预设无效")
    if resolution == 'custom' and not (width and height):
        raise HTTPException(422, "请填写自定义宽度与高度")
    try:
        profile = video_profile(user_id, profile_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return _resource_estimate(profile, ratio, resolution, seconds, _available_resources(),
                              (width, height) if resolution == 'custom' else None)


@router.put("/connection")
def save_connection(payload: ConnectionRequest, request: Request) -> dict[str, Any]:
    user_id = int(require_user(request)["id"])
    value = _validate_connection(payload.model_dump())
    _write_json(_user_root(user_id) / "connection.json", value)
    return value


@router.post("/connection/test")
def test_connection(payload: ConnectionRequest, request: Request) -> dict[str, Any]:
    require_user(request)
    value = _validate_connection(payload.model_dump())
    if value.get("mode") == "managed":
        from .managed_comfyui import ensure_ready
        try:
            value = {**DEFAULT_CONNECTION, "base_url": ensure_ready()}
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
    started = time.monotonic()
    try:
        response = requests.get(_url(value, "/system_stats"), timeout=8)
        response.raise_for_status()
        stats = response.json()
        nodes = requests.get(_url(value, "/object_info"), timeout=15)
        nodes.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(400, f"无法连接 ComfyUI：{exc}") from exc
    return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "node_count": len(nodes.json()), "system": stats.get("system", {})}


@router.put("/profiles")
def save_profile(payload: ProfileRequest, request: Request) -> dict[str, Any]:
    user_id = int(require_user(request)["id"])
    from .managed_comfyui import builtin_profiles, maintenance_enabled
    builtin_ids = {item["id"] for item in builtin_profiles()}
    if not maintenance_enabled() and (payload.engine == "managed" or payload.id in builtin_ids):
        raise HTTPException(403, "内置视频工作流由 OCV 维护，请通过 Launcher 更新；自定义工作流请使用外部 ComfyUI。")
    try:
        graph = _api_graph(payload.workflow)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    profile_id = payload.id if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", payload.id or "") else uuid.uuid4().hex[:16]
    value = {**payload.model_dump(), "id": profile_id, "workflow": graph, "updated_at": time.time()}
    # Validate every configured binding before persisting a broken preset.
    mappings = payload.mappings
    for label, binding in _profile_bindings(mappings):
        try:
            _binding(graph, binding, label, required=label == "提示词")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if mappings.output_node_id and mappings.output_node_id not in graph:
        raise HTTPException(400, f"输出节点 {mappings.output_node_id} 不存在")
    if payload.kind == "video":
        try:
            validate_video_inputs(value, input_mode=payload.video_input_mode,
                                  reference_count=len(mappings.reference_images) if payload.video_input_mode == "composition_reference" else 0,
                                  has_last=payload.video_input_mode == "first_last")
            _patch_opening_guide(copy.deepcopy(graph), mappings, enabled=payload.h3_opening_guide)
            if payload.h3_opening_guide:
                _require_opening_guide_support(_connection(user_id))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    values = _profiles(user_id)
    values = [value] + [item for item in values if item.get("id") != profile_id]
    _write_json(_user_root(user_id) / "profiles.json", values[:50])
    return {**value, "workflow": None, "nodes": _node_summary(graph)}


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str, request: Request) -> dict[str, Any]:
    user_id = int(require_user(request)["id"])
    value = next((item for item in _profiles(user_id) if item.get("id") == profile_id), None)
    if not value:
        raise HTTPException(404, "工作流预设不存在")
    return {**value, "nodes": _node_summary(_api_graph(value["workflow"]))}


@router.post("/capture-latest")
def capture_latest(request: Request) -> dict[str, Any]:
    """Read the last queued graph, avoiding a manual API-workflow export."""
    user_id = int(require_user(request)["id"])
    connection = _connection(user_id)
    collection_path = connection["history_path"].split("/{prompt_id}", 1)[0].rstrip("/") or "/history"
    try:
        response = requests.get(_url(connection, collection_path), params={"max_items": 30}, timeout=30)
        response.raise_for_status()
        history = response.json()
    except requests.RequestException as exc:
        raise HTTPException(400, f"读取 ComfyUI 历史失败：{exc}") from exc
    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for prompt_id, item in (history.items() if isinstance(history, dict) else []):
        prompt = item.get("prompt") if isinstance(item, dict) else None
        graph = prompt[2] if isinstance(prompt, list) and len(prompt) > 2 and isinstance(prompt[2], dict) else None
        if graph:
            sequence = float(prompt[0]) if isinstance(prompt[0], (int, float)) else 0
            candidates.append((sequence, str(prompt_id), graph))
    if not candidates:
        raise HTTPException(404, "ComfyUI 暂无可读取的执行历史；请先在 ComfyUI 成功提交一次工作流")
    sequence, prompt_id, graph = max(candidates, key=lambda item: item[0])
    return {"prompt_id": prompt_id, "sequence": sequence, "workflow": graph, "nodes": _node_summary(graph)}


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str, request: Request) -> dict[str, bool]:
    user_id = int(require_user(request)["id"])
    values = _profiles(user_id)
    if any(item.get("id") == profile_id and item.get("managed_builtin") for item in values):
        raise HTTPException(403, "内置工作流由 OCV 维护，无需删除；可选择其他生成方式。")
    remaining = [value for value in values if value.get("id") != profile_id]
    if len(remaining) == len(values):
        raise HTTPException(404, "工作流预设不存在")
    _write_json(_user_root(user_id) / "profiles.json", remaining)
    return {"deleted": True}


@router.post("/jobs")
async def create_job(request: Request, profile_id: str = Form(...), prompt: str = Form(...), duration: float = Form(5), width: int = Form(1280), height: int = Form(720), seed: int = Form(-1), reference: UploadFile | None = File(default=None), reference_audio: UploadFile | None = File(default=None)) -> dict[str, Any]:
    user_id = int(require_user(request)["id"])
    profile = next((value for value in _profiles(user_id) if value.get("id") == profile_id), None)
    if not profile:
        raise HTTPException(404, "工作流预设不存在")
    if not prompt.strip():
        raise HTTPException(400, "请填写提示词")
    if profile.get("kind") == "video":
        try:
            validate_video_inputs(profile, input_mode=profile.get("video_input_mode") or "single_first",
                                  reference_count=0, has_last=False)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    job_id = uuid.uuid4().hex
    path = _job_path(user_id, job_id)
    path.mkdir(parents=True, exist_ok=False)
    source = None
    if reference is not None and reference.filename:
        suffix = Path(reference.filename).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise HTTPException(400, "参考图仅支持 PNG、JPG 和 WebP")
        source = path / ("reference" + suffix)
        with source.open("wb") as stream:
            shutil.copyfileobj(reference.file, stream)
    audio_source = None
    if reference_audio is not None and reference_audio.filename:
        suffix = Path(reference_audio.filename).suffix.lower()
        if suffix not in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
            raise HTTPException(400, "参考音频仅支持 WAV、MP3、FLAC、M4A 和 OGG")
        audio_source = path / ("reference_audio" + suffix)
        with audio_source.open("wb") as stream:
            shutil.copyfileobj(reference_audio.file, stream)
    actual_seed = seed if seed >= 0 else uuid.uuid4().int % (2**63 - 1)
    record = {"id": job_id, "profile_id": profile_id, "profile_name": profile.get("name"), "status": "running", "message": "准备提交", "progress": 0, "created_at": time.time(), "prompt": prompt, "duration": duration, "width": width, "height": height, "seed": actual_seed}
    _save_job(path, record)
    threading.Thread(target=_execute, args=(user_id, job_id, profile, source, audio_source, {"prompt": prompt, "duration": max(.1, duration), "width": max(32, width), "height": max(32, height), "seed": actual_seed}), daemon=True).start()
    return record


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    user_id = int(require_user(request)["id"])
    record = _read_json(_job_path(user_id, job_id) / "record.json", None)
    if not record:
        raise HTTPException(404, "任务不存在")
    if record.get("result_name"):
        record["result_url"] = f"/api/comfyui/jobs/{job_id}/result"
    return record


@router.get("/jobs/{job_id}/result")
def get_job_result(job_id: str, request: Request) -> FileResponse:
    user_id = int(require_user(request)["id"])
    path = _job_path(user_id, job_id)
    record = _read_json(path / "record.json", None)
    result = path / str((record or {}).get("result_name") or "")
    if not result.is_file() or result.parent != path:
        raise HTTPException(404, "生成结果不存在")
    return FileResponse(result, filename=result.name, headers={"Cache-Control": "no-store"})


@router.post("/interrupt")
def interrupt(request: Request) -> dict[str, bool]:
    user_id = int(require_user(request)["id"])
    if os.environ.get(GUARD_ENV_NAME, "").strip():
        raise HTTPException(409, "ComfyUI guard 已启用，禁止全局终止共享 GPU 任务；停止本地等待不会终止后台任务。")
    connection = _connection(user_id)
    try:
        response = requests.post(_url(connection, connection["interrupt_path"]), timeout=15)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(400, f"终止请求失败：{exc}") from exc
    return {"interrupted": True}
