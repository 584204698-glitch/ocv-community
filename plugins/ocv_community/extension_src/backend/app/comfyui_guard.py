"""Optional, durable submission through an administrator-installed GPU guard.

The guard owns GPU exclusion until its terminal-history cleanup has finished.
OCV waits for the response identity, then for that same guard to exit before a
storyboard worker advances. It never terminates the guard on stop or disconnect.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin
import uuid

import requests


ENV_NAME = "OCV_COMFYUI_GUARD_SCRIPT"
TARGET = "http://127.0.0.1:8188"
_PROCESSES: dict[str, subprocess.Popen] = {}
_THREAD_LOCK = threading.RLock()
_SAFE_REFUSALS = {
    "GPU_LOCK_BUSY", "COMFYUI_NOT_IDLE", "OTHER_GPU_PROCESS",
    "H3_RUNTIME_CONTRACT_ERROR", "WORKFLOW_INVALID", "RUNTIME_CONTRACT_ERROR",
    "USED_NODE_CLASS_MISSING", "OFFICIAL_LOADER_CONTRACT_FAILED",
    "OFFICIAL_CONDITIONING_REQUIRED", "OUTPUT_NODE_REQUIRED", "H3_RUNTIME_MISMATCH",
    "UNKNOWN_APPROVED_RUNTIME", "CANDIDATE_APPROVAL_REQUIRED",
    "LOW_PRECISION_REQUIRES_CANDIDATE_APPROVAL",
    "CANDIDATE_APPROVAL_MALFORMED", "CANDIDATE_APPROVAL_OUT_OF_SCOPE",
    "CANDIDATE_CLASS_SCOPE_MISMATCH", "PRECISION_MARKERS_NOT_APPROVED",
    "CANDIDATE_WORKFLOW_HASH_MISMATCH", "CANDIDATE_APPROVAL_EXPIRED",
}


class GuardSubmissionError(ValueError):
    """A guard refusal or uncertain submission that must not fall back to HTTP."""


class GuardWaitingStopped(RuntimeError):
    """Only the local wait stopped; the detached guard still owns submission."""


def stop_submission(connection, directory):
    from .comfyui_cancel import cancel
    try:
        result=cancel(connection,directory)
        error=GuardWaitingStopped(result['message'])
        error.terminal=result['status'] in {'cancelled','not_submitted'}
    except (ValueError,OSError,requests.RequestException) as exc:
        error=GuardWaitingStopped('本镜取消尚未完成：'+str(exc)+'；原任务记录已保留，请重试停止。')
        error.terminal=False
    raise error


def _uncertain(detail: str) -> GuardSubmissionError:
    return GuardSubmissionError(
        f"ComfyUI guard 提交身份尚未确认，已阻止重复提交：{detail}；请保留提交记录并核查服务端任务")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def _write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _lock_path(directory: Path) -> Path:
    # Attempts of the same shot must not race each other into two submissions.
    if re.fullmatch(r"attempt_\d+", directory.parent.name):
        return directory.parent.parent / ".comfyui_guard.lock"
    return directory / ".lock"


@contextmanager
def _submission_lock(directory: Path):
    _private_dir(directory)
    path = _lock_path(directory)
    with _THREAD_LOCK:
        descriptor = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            if os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _read_receipt(directory: Path) -> dict | None:
    path = directory / "receipt.json"
    if not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or receipt.get("version") != 1
                or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("task_id", "")))
                or not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("graph_sha256", "")))
                or receipt.get("base_url") != TARGET or receipt.get("prompt_path") != "/prompt"
                or receipt.get("state") not in {"starting", "running", "submitted", "refused", "uncertain", "start_failed"}):
            raise ValueError("invalid receipt")
        return receipt
    except (ValueError, OSError) as exc:
        raise _uncertain(f"提交凭据损坏：{path}") from exc


def _response_id(directory: Path, receipt: dict) -> str:
    recorded = receipt.get("prompt_id") or ""
    if not isinstance(recorded, str):
        raise _uncertain("凭据中的 prompt_id 无效")
    response_path = directory / "response.json"
    received = ""
    if response_path.exists():
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # h3_guard writes the response non-atomically; retry while it lives.
            payload = None
        if payload is not None:
            if not isinstance(payload, dict) or not isinstance(payload.get("prompt_id"), str) or not payload["prompt_id"].strip():
                raise _uncertain("guard 响应缺少有效 prompt_id")
            received = payload["prompt_id"].strip()
    if recorded and received and recorded != received:
        raise _uncertain("凭据与响应的 prompt_id 冲突")
    return received or recorded


def get_guard_prompt_id(submission_dir: Path, *, require_resolved: bool = False) -> str:
    """Read an exact attempt's identity without waiting or requiring configuration."""
    directory = Path(submission_dir)
    receipt = _read_receipt(directory)
    if receipt is None:
        return ""
    prompt_id = _response_id(directory, receipt)
    if prompt_id:
        return prompt_id
    if receipt["state"] == "uncertain":
        raise _uncertain(str(receipt.get("detail") or "guard 未返回任务 ID"))
    if require_resolved and receipt["state"] not in {"refused", "start_failed"}:
        refusal = _refusal(directory) if not _alive(receipt) else None
        if not refusal:
            raise _uncertain("guard 凭据已创建，正在等待任务 ID；稍后重试将恢复同一任务")
    return ""


def _validate_target(connection: dict) -> None:
    if connection.get("base_url") != TARGET or connection.get("prompt_path") != "/prompt":
        raise GuardSubmissionError(
            "启用 ComfyUI guard 时只允许 http://127.0.0.1:8188 和 /prompt；不会将其他服务器的任务转发到本机")


def validate_guard_connection(connection: dict, submission_dir: Path) -> None:
    """Validate the target of an existing receipt without requiring current opt-in."""
    if _read_receipt(Path(submission_dir)) is not None:
        _validate_target(connection)


def _configured_script(connection: dict) -> Path | None:
    configured = os.environ.get(ENV_NAME, "").strip()
    if not configured:
        return None
    _validate_target(connection)
    path = Path(configured)
    if not path.is_absolute() or not path.is_file():
        raise GuardSubmissionError(f"{ENV_NAME} 必须是管理员配置的现有脚本绝对路径")
    return path.resolve()


def _process_identity(pid: int, *, require_known: bool = False) -> str:
    if sys.platform.startswith("linux"):
        try:
            process_stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return ""
        except OSError as exc:
            if require_known:
                raise GuardWaitingStopped("无法确认原 ComfyUI guard 是否已退出；保留原任务，可稍后恢复查询。") from exc
            return ""
        try:
            stat = process_stat.rsplit(")", 1)[1].split()
            if stat[0] == "Z":
                return ""
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if require_known and not boot:
                raise GuardWaitingStopped("无法确认原 ComfyUI guard 的进程身份；保留原任务，可稍后恢复查询。")
            return f"{boot}:{stat[19]}"
        except (OSError, IndexError) as exc:
            if require_known:
                raise GuardWaitingStopped("无法确认原 ComfyUI guard 是否已退出；保留原任务，可稍后恢复查询。") from exc
            return ""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            if require_known and ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: no such PID.
                raise GuardWaitingStopped("无法读取原 ComfyUI guard 进程；保留原任务，可稍后恢复查询。")
            return ""
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                if require_known:
                    raise GuardWaitingStopped("无法确认原 ComfyUI guard 的退出状态；保留原任务，可稍后恢复查询。")
                return ""
            if code.value != 259:
                return ""
            values = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                if require_known:
                    raise GuardWaitingStopped("无法确认原 ComfyUI guard 的进程身份；保留原任务，可稍后恢复查询。")
                return ""
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    if require_known:
        raise GuardWaitingStopped("当前平台无法确认原 ComfyUI guard 已退出；保留原任务，可稍后恢复查询。")
    return ""  # Unknown process identity fails closed during restart recovery.


def _alive(receipt: dict, *, require_known: bool = False) -> bool:
    process = _PROCESSES.get(receipt["task_id"])
    if process is not None:
        try:
            return process.poll() is None
        except OSError as exc:
            if require_known:
                raise GuardWaitingStopped("无法确认原 ComfyUI guard 的退出状态；保留原任务，可稍后恢复查询。") from exc
            raise
    pid = receipt.get("pid")
    identity = receipt.get("process_identity")
    identified = type(pid) is int and pid > 0 and bool(identity)
    if require_known and (not isinstance(identity, str) or not re.fullmatch(
            r"(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}:\d+|\d+)", identity)):
        identified = False
    if require_known and not identified:
        raise GuardWaitingStopped("原 ComfyUI guard 的进程身份缺失；视频与提交凭据已保留，可稍后恢复查询。")
    if not identified:
        return False
    current = _process_identity(pid, require_known=True) if require_known else _process_identity(pid)
    return current == identity


def wait_for_guard_release(submission_dir: Path, prompt_id: str, *, progress=None, should_stop=None) -> None:
    """Wait only for this downloaded result's guard; never submit or interrupt.

    A receipt is authoritative even if guard opt-in or the preset has since
    changed. No receipt means a legacy/direct submission and needs no wait.
    Unverifiable ownership is a resumable local stop, never proof of release.
    """
    directory = Path(submission_dir).resolve()
    try:
        receipt = _read_receipt(directory)
        if receipt is None:
            return
        if not prompt_id or _response_id(directory, receipt) != prompt_id:
            raise _uncertain("下载结果与 guard 提交身份不一致")
    except (GuardSubmissionError, OSError) as exc:
        raise GuardWaitingStopped("视频已下载，但无法确认原 guard 的收尾身份；保留原任务并停止推进后续镜头。") from exc
    announced = False
    while True:
        if should_stop and should_stop():
            raise GuardWaitingStopped("已停止本地等待 guard 收尾；视频与原任务 ID 已保留，后台守锁不受影响，可恢复查询。")
        if not _alive(receipt, require_known=True):
            return
        if progress and not announced:
            progress("ComfyUI 视频已下载，正在等待本次 guard 收尾并释放 GPU 锁，再继续下一镜")
            announced = True
        time.sleep(0.2)


def assert_submission_released(submission_dir: Path, prompt_id: str) -> None:
    """Bounded readiness check before explicitly replacing a terminal attempt."""
    directory=Path(submission_dir).resolve()
    receipt=_read_receipt(directory)
    if receipt is None:return
    if _response_id(directory,receipt)!=prompt_id:
        raise GuardWaitingStopped('原任务与提交凭据不一致，未重新生成')
    if _alive(receipt,require_known=True):
        raise GuardWaitingStopped('原任务已经结束，但守锁尚在收尾；请稍后重新生成')


def _refusal(directory: Path) -> dict | None:
    path = directory / "guard.log"
    if not path.exists():
        return None
    if path.stat().st_size > 1024 * 1024:
        # Never begin parsing in an arbitrary truncated JSON subtree.
        return None
    with path.open("rb") as stream:
        content = stream.read().decode("utf-8", errors="replace")
    decoder = json.JSONDecoder()
    offset = 0
    while True:
        match = re.search(r"(?m)^\{", content[offset:])
        if match is None:
            return None
        start = offset + match.start()
        try:
            value, offset = decoder.raw_decode(content, start)
        except ValueError:
            # A partial outer JSON object cannot turn its complete nested
            # response into a definitive guard refusal.
            return None
        if isinstance(value, dict):
            code = value.get("code")
            if (value.get("prompt_id") or code in {"SUBMIT_TRANSPORT_ERROR", "MISSING_PROMPT_ID"}
                    or str(code).startswith("PROMPT_") and code != "PROMPT_REJECTED"):
                return None
            if code == "PROMPT_REJECTED":
                # The guard uses this code for every HTTPError, including 5xx
                # after a potentially successful enqueue.  Only definitive
                # client/admission refusals establish that retry is safe.
                return value if value.get("ok") is False and value.get("http_status") in {
                    400, 401, 403, 404, 405, 413, 415, 422, 423, 429,
                } else None
            if value.get("ok") is False and code in _SAFE_REFUSALS:
                return value
    return None


def _check_previous_attempts(directory: Path) -> None:
    if not re.fullmatch(r"attempt_\d+", directory.parent.name):
        return
    for sibling in directory.parent.parent.glob("attempt_*/comfyui_submission"):
        if sibling == directory:
            continue
        receipt = _read_receipt(sibling)
        if receipt is None or receipt["state"] in {"refused", "start_failed"}:
            continue
        if not _response_id(sibling, receipt) and not _alive(receipt) and _refusal(sibling):
            continue
        # A received ID whose callback never completed is still owned by the
        # earlier attempt.  Do not hide that crash by creating a new attempt.
        if not receipt.get("delivered"):
            cancelled=sibling/'cancel.json'
            if cancelled.exists() and not _alive(receipt,require_known=True):
                report=json.loads(cancelled.read_text(encoding='utf8'))
                if report.get('terminal') and report.get('status')=='cancelled' and report.get('prompt_id')==_response_id(sibling,receipt):
                    continue
            raise _uncertain(f"前次 {sibling.parent.name} 提交尚待恢复")


def _deliver(directory: Path, prompt_id: str, on_submitted) -> str:
    with _submission_lock(directory):
        receipt = _read_receipt(directory)
        receipt.update(prompt_id=prompt_id, state="submitted")
        _write_json(directory / "receipt.json", receipt)
    if on_submitted:
        on_submitted(prompt_id)
    with _submission_lock(directory):
        receipt = _read_receipt(directory)
        receipt["delivered"] = True
        _write_json(directory / "receipt.json", receipt)
    return prompt_id


def _await_submission(directory: Path, on_submitted=None, progress=None, should_stop=None) -> str:
    announced = False
    while True:
        with _submission_lock(directory):
            receipt = _read_receipt(directory)
        prompt_id = _response_id(directory, receipt)
        if prompt_id:
            return _deliver(directory, prompt_id, on_submitted)
        if receipt["state"] in {"refused", "start_failed"}:
            raise GuardSubmissionError(str(receipt.get("detail") or "ComfyUI guard 拒绝提交"))
        if receipt["state"] == "uncertain":
            raise _uncertain(str(receipt.get("detail") or "guard 未返回任务 ID"))
        if should_stop and should_stop():
            stop_submission({'base_url':TARGET,'prompt_path':'/prompt'},directory)
        if not _alive(receipt):
            # Check once more after process exit before classifying its result.
            prompt_id = _response_id(directory, receipt)
            if prompt_id:
                return _deliver(directory, prompt_id, on_submitted)
            refusal = _refusal(directory)
            detail = (f"ComfyUI guard 拒绝提交 [{refusal['code']}]：{refusal.get('message') or refusal.get('detail') or refusal}"
                      if refusal else "guard 已退出或启动状态不明，但未留下可确认的任务 ID")
            with _submission_lock(directory):
                receipt = _read_receipt(directory)
                receipt.update(state="refused" if refusal else "uncertain", detail=detail)
                _write_json(directory / "receipt.json", receipt)
            if refusal:
                raise GuardSubmissionError(detail)
            raise _uncertain(detail)
        if progress and not announced:
            progress("正在等待 ComfyUI guard 确认任务 ID；已保留提交身份，不会重复提交")
            announced = True
        time.sleep(0.2)


def recover_guard_submission(connection: dict, submission_dir: Path, *, on_submitted=None, progress=None, should_stop=None) -> str:
    """Resume before rebuilding/uploading inputs; an uncertain receipt never retries."""
    directory = Path(submission_dir).resolve()
    if _read_receipt(directory) is not None:
        _validate_target(connection)
        return _await_submission(directory, on_submitted, progress, should_stop)
    _check_previous_attempts(directory)
    _configured_script(connection)  # Invalid configuration fails before uploading.
    return ""


def submit_prompt(connection: dict, graph: dict, client_id: str, submission_dir: Path,
                  *, on_submitted=None, progress=None, should_stop=None) -> str:
    """Submit once via direct HTTP or the explicitly configured local GPU guard."""
    directory = Path(submission_dir).resolve()
    previous = _read_receipt(directory)
    script = None if previous else _configured_script(connection)
    if previous is not None:
        _validate_target(connection)
        if previous["graph_sha256"] != _digest(graph):
            raise _uncertain("当前工作流与已冻结的提交记录不同")
        return _await_submission(directory, on_submitted, progress, should_stop)
    _check_previous_attempts(directory)
    if script is None:
        response = requests.post(urljoin(connection["base_url"] + "/", connection["prompt_path"].lstrip("/")),
                                 json={"prompt": graph, "client_id": client_id}, timeout=60)
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(f"ComfyUI 拒绝工作流：{detail}")
        prompt_id = str(response.json().get("prompt_id") or "")
        if not prompt_id:
            raise RuntimeError("ComfyUI 未返回 prompt_id")
        if on_submitted:
            on_submitted(prompt_id)
        return prompt_id
    with _submission_lock(directory):
        previous = _read_receipt(directory)
        if previous is not None:
            if previous["graph_sha256"] != _digest(graph):
                raise _uncertain("当前工作流与已冻结的提交记录不同")
        else:
            _check_previous_attempts(directory)
            if (directory / "response.json").exists():
                raise _uncertain("存在无凭据的旧 guard 响应")
            payload = {"prompt": graph, "client_id": client_id}
            receipt = {"version": 1, "task_id": uuid.uuid4().hex, "state": "starting",
                       "base_url": TARGET, "prompt_path": "/prompt", "script": str(script),
                       "graph_sha256": _digest(graph), "workflow_sha256": _digest(payload),
                       "created_at": time.time(), "delivered": False}
            _write_json(directory / "workflow.json", payload)
            _write_json(directory / "receipt.json", receipt)
            # Precreate the response with restrictive permissions; the guard's
            # ordinary write_text then retains that mode.
            descriptor = os.open(str(directory / "response.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            log_descriptor = os.open(str(directory / "guard.log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            argv = [sys.executable, str(script), "submit", "--owner", "ocv", "--task-id", receipt["task_id"],
                    "--workflow", str(directory / "workflow.json"), "--response-file", str(directory / "response.json"),
                    "--timeout", "86400", "--poll", "2"]
            options = {"stdin": subprocess.DEVNULL, "shell": False, "close_fds": True}
            if os.name == "nt":
                options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
            try:
                with os.fdopen(log_descriptor, "ab", buffering=0) as log:
                    process = subprocess.Popen(argv, stdout=log, stderr=log, **options)
            except OSError as exc:
                receipt.update(state="start_failed", detail=f"无法启动 ComfyUI guard：{exc}")
                _write_json(directory / "receipt.json", receipt)
                raise GuardSubmissionError(receipt["detail"]) from exc
            _PROCESSES[receipt["task_id"]] = process
            receipt.update(state="running", pid=process.pid, process_identity=_process_identity(process.pid))
            _write_json(directory / "receipt.json", receipt)
    return _await_submission(directory, on_submitted, progress, should_stop)
