"""Call an administrator-installed TTS guard; never own the shared GPU lock.

The existing machine TTS guard owns queue/resource admission, the inference
child and lock release. This adapter only checks its configured file identity
and builds argv. It neither edits shared state nor uses H3 acquire as a generic
command runner. Portable installations without a shared guard keep their
existing local execution path.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sys


def tts_guard_script() -> Path | None:
    configured = os.environ.get("INDEXTTS25_GPU_GUARD", "").strip()
    expected = os.environ.get("INDEXTTS25_GPU_GUARD_SHA256", "").strip().lower()
    shared = os.environ.get("OCV_COMFYUI_GUARD_SCRIPT", "").strip()
    if not configured and not shared and not expected:
        return None
    script = Path(configured)
    if not configured or not script.is_absolute() or script.is_symlink() or not script.is_file():
        raise RuntimeError("IndexTTS 共享 GPU guard 不可用，已拒绝无保护推理")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RuntimeError("IndexTTS 共享 GPU guard 缺少已核验版本摘要")
    if hashlib.sha256(script.read_bytes()).hexdigest() != expected:
        raise RuntimeError("IndexTTS 共享 GPU guard 版本已变化，已拒绝推理")
    return script


def guarded_tts_command(command: list[str], *, owner: int | None, task_id: str) -> tuple[list[str], bool]:
    script = tts_guard_script()
    if script is None:
        return list(command), False
    if not command or type(owner) is not int or owner < 1 or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", task_id):
        raise RuntimeError("IndexTTS guard 的账号或任务标识无效")
    return [sys.executable, "-B", str(script), "--owner", f"ocv-user-{owner}",
            "--task-id", task_id, "--", *command], True
