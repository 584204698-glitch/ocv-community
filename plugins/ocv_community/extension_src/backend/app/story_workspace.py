# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-scoped, versioned story drafts. Media work starts in the existing studio.

Like the local OCV task runner this router runs in one server process. A saved
language task from a previous process is interrupted, never replayed on a GET.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import require_user
from .gemini_client import generate_gemini_text
from .story_development import (
    develop_story, plan_story, propose_rewrite, review_story, validate_plan, validate_review, StoryDirectionsRejected,
)
from .story_production import make_story_snapshot, validate_story_snapshot
from .story_writer import (
    STORY_PRESETS, STORY_WRITER_SYSTEM_PROMPT, canonical_story_brief,
    parse_story_writer_response, story_text_sha256, story_writer_user_prompt,
    validate_story_text, story_writer_system_prompt,
)

router = APIRouter(prefix="/api/stories", tags=["stories"])
# /workspace is served as static content by the original application. Drafts
# belong outside that mount and are available only through authenticated routes.
ROOT = Path(__file__).resolve().parents[2] / "runtime" / "story_drafts"
LOCK = threading.RLock()
PROCESS_ID = uuid.uuid4().hex
ACTIVE: set[tuple[int, str, str]] = set()


class StoryFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["idea", "topics", "manuscript"] = "idea"
    idea: str = Field(default="", max_length=4000)
    preset: str = Field(default="general", max_length=60)
    duration_seconds: int = Field(default=60, ge=15, le=600, strict=True)
    title: str = Field(default="", max_length=80)
    story: str = Field(default="", max_length=12000)


class CreateStory(StoryFields):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")


class SaveStory(StoryFields):
    revision: int = Field(ge=1, strict=True)


class StoryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)


class GenerateStory(StoryRevision):
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")


class DevelopStory(GenerateStory):
    direction_id: str | None = Field(default=None, min_length=1, max_length=64)


class RewriteStory(GenerateStory):
    start: int = Field(ge=0, le=12000, strict=True)
    end: int = Field(ge=1, le=12000, strict=True)
    instruction: str = Field(min_length=3, max_length=2000)


class RewriteDecision(StoryRevision):
    proposal_id: str = Field(min_length=1, max_length=80)


class StorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    story_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    story_revision: int = Field(ge=1, strict=True)
    story_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    adoption_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


def _path(owner: int, identity: str) -> Path:
    if owner < 1 or not re.fullmatch(r"[a-f0-9]{32}", identity):
        raise HTTPException(404, "故事不存在，或当前账号没有访问权限")
    return ROOT / str(owner) / f"{identity}.json"


def _write(owner: int, record: dict) -> None:
    path = _path(owner, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _touch(record: dict) -> None:
    record["revision"] += 1
    record["updated_at"] = time.time()


def _load(owner: int, identity: str) -> dict:
    path = _path(owner, identity)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(404, "故事不存在，或当前账号没有访问权限") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "故事记录暂时无法读取，请保留当前编辑并稍后重试") from exc
    if record.get("owner_id") != owner or record.get("id") != identity:
        raise HTTPException(404, "故事不存在，或当前账号没有访问权限")
    task = record.get("task", {})
    if task.get("status") == "running" and task.get("process_id") != PROCESS_ID:
        task.update(status="interrupted", error="服务重启中断了写作。原稿已保留，可手动重新生成。", finished_at=time.time())
        record["status"] = "interrupted"
        _touch(record)
        _write(owner, record)
    return record


def _public(record: dict) -> dict:
    result = copy.deepcopy(record)
    for key in ("owner_id", "create_payload", "generation_requests", "operation_requests", "adoption_snapshots"):
        result.pop(key, None)
    result["brief_sha256"] = _brief_hash(record["brief"])
    result.get("task", {}).pop("process_id", None)
    result.get("task", {}).pop("worker_started", None)
    result.get("task", {}).pop("input_snapshot", None)
    return result


def _brief_hash(brief: dict) -> str:
    return hashlib.sha256(json.dumps(brief, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _invalidate_editorial(record: dict, *, brief_changed: bool = False) -> None:
    if record.get("review"):
        record["review"]["status"] = "stale"
        record["editorial_review"] = "stale"
    else:
        record["editorial_review"] = "not_performed"
    if record.get("rewrite_proposal", {}).get("status") == "pending":
        record["rewrite_proposal"]["status"] = "stale"
    if brief_changed and record.get("development"):
        record["development"]["status"] = "stale"


def _current_review(review: dict, brief: dict, story: str) -> dict:
    # Even alternate adapters must return real paragraph evidence.
    checked = validate_review(review, story)
    return {**checked, "status": "current", "story_sha256": story_text_sha256(story),
            "brief_sha256": _brief_hash(brief)}


def _no_pending_rewrite(record: dict) -> None:
    if record.get("rewrite_proposal", {}).get("status") == "pending":
        raise HTTPException(409, "有一份修改建议等待处理，请先采用或丢弃，再开始下一次创作")


def _check_revision(record: dict, revision: int) -> None:
    if record["revision"] != revision:
        raise HTTPException(409, "故事已更新，请重新读取最新版本；当前编辑不会自动覆盖服务器内容")


def _check_idle(record: dict) -> None:
    if record.get("task", {}).get("status") == "running":
        raise HTTPException(409, "故事正在生成，请完成后再保存或采用；可以先切换到其他项目")


def _fields(payload: StoryFields) -> tuple[dict, str, str]:
    try:
        brief = canonical_story_brief(
            idea=payload.idea, preset=payload.preset,
            duration_seconds=payload.duration_seconds, intent=payload.intent,
        )
        if payload.story.strip():
            validate_story_text(payload.story)
        # Preserve an author's pasted text verbatim, including paragraph and
        # outer whitespace. Hashing may canonicalize without changing the draft.
        story = payload.story
    except ValueError as exc:
        raise HTTPException(422, "请检查故事想法、题材和正文：正文需为 20–12000 字的可朗读文字，不能混入制作指令") from exc
    return brief, payload.title.strip(), story


def _version(record: dict, origin: str) -> None:
    record["story_revision"] += 1
    record["story_sha256"] = story_text_sha256(record["story"])
    record["versions"].append({
        "story_revision": record["story_revision"], "title": record["title"],
        "story": record["story"], "story_sha256": record["story_sha256"],
        "created_at": time.time(), "origin": origin,
    })


def _generate_story(brief: dict) -> dict:
    raw = generate_gemini_text(
        system_prompt=story_writer_system_prompt(brief),
        user_prompt=story_writer_user_prompt(brief), temperature=0.7,
        response_mime_type="application/json", json_root="object", max_output_tokens=10000,
    )
    return parse_story_writer_response(raw)


def _run_generation(owner: int, identity: str, request_id: str) -> None:
    key = (owner, identity, request_id)
    owns_worker = False
    try:
        with LOCK:
            record = _load(owner, identity)
            task = record["task"]
            if task.get("request_id") != request_id or task.get("status") != "running" or task.get("worker_started"):
                return
            task["worker_started"] = True
            _write(owner, record)
            owns_worker = True
            brief = copy.deepcopy(record["brief"])
            operation = task.get("operation", "generate")
            if operation != "generate":
                snapshot = copy.deepcopy(task["input_snapshot"])
        if operation != "generate":
            _run_development(owner, identity, request_id, operation, snapshot)
            return
        try:
            generated = _generate_story(brief)
            # This boundary also guards alternate writer adapters.
            generated = parse_story_writer_response(json.dumps({
                field: generated[field] for field in ("title", "story", "synopsis", "characters", "scenes")
            }, ensure_ascii=False))
            error = ""
        except Exception as exc:
            # Provider errors can contain API keys, request bodies and private
            # URLs. Persist a useful category, never the upstream exception.
            error = ("写作返回的内容不完整或不符合正文格式，原稿已保留。可重新生成。"
                     if isinstance(exc, ValueError) else
                     "写作服务未能完成请求，原稿已保留。请检查接口与服务配置后重试。")
            generated = None
        with LOCK:
            record = _load(owner, identity)
            task = record["task"]
            if task.get("request_id") != request_id or task.get("status") != "running" or task.get("process_id") != PROCESS_ID:
                return
            if generated is not None:
                _invalidate_editorial(record)
                record.update({key: generated[key] for key in ("title", "story", "synopsis", "characters", "scenes")})
                if not record["versions"] or record["story_sha256"] != story_text_sha256(record["story"]):
                    _version(record, "generated")
                record.update(derived_status="current", derived_from_story_sha256=record["story_sha256"],
                              character_identity_status="suggested", status="ready")
                task.update(status="completed", error="", finished_at=time.time())
            else:
                record["status"] = "failed"
                task.update(status="failed", error=error, finished_at=time.time())
            _touch(record)
            _write(owner, record)
    finally:
        if owns_worker:
            with LOCK:
                ACTIVE.discard(key)


def _start_generation(owner: int, identity: str, request_id: str) -> None:
    threading.Thread(target=_run_generation, args=(owner, identity, request_id), daemon=True,
                     name=f"story-{identity[:8]}").start()


def _task_matches(record: dict, request_id: str) -> bool:
    task = record.get("task", {})
    return (task.get("request_id") == request_id and task.get("status") == "running"
            and task.get("process_id") == PROCESS_ID)


def _snapshot_matches(record: dict, snapshot: dict) -> bool:
    return (record["story_revision"] == snapshot["story_revision"]
            and record["story"] == snapshot["story"] and record["brief"] == snapshot["brief"])


def _model_call(**kwargs) -> str:
    return generate_gemini_text(**kwargs, response_mime_type="application/json", json_root="object")


def _run_development(owner: int, identity: str, request_id: str, operation: str, snapshot: dict) -> None:
    def progress(stage: str, detail: dict) -> None:
        with LOCK:
            record = _load(owner, identity)
            if not _task_matches(record, request_id):
                return
            task = record["task"]
            task.update(stage=stage, message=detail.get("message", ""),
                        call_count=detail.get("call_index", task.get("call_count", 0)))
            # Only parsed artifacts from our runner cross this boundary; never
            # persist a raw provider response, request, or exception.
            if detail.get("event") == "completed":
                fields = {"plan", "directions", "draft", "review", "reviews", "story", "replacement",
                          "round", "story_sha256", "revision_rounds", "editorial_review", "audience_checks", "selection_rejection"}
                task["checkpoints"].append({"stage": stage, "at": time.time(),
                    **{key: copy.deepcopy(value) for key, value in detail.items() if key in fields}})
            _touch(record)
            _write(owner, record)

    try:
        brief, body = snapshot["brief"], snapshot["story"]
        options = {"call": _model_call, "progress": progress}
        if operation == "directions":
            result = validate_plan(plan_story(brief, **options))
        elif operation == "develop":
            result = develop_story(brief, plan=snapshot.get("plan"),
                                   direction_id=snapshot.get("direction_id"), **options)
            plan = result["plan"]
            if plan is None:
                if brief["preset"] != "picturebook" or snapshot.get("plan") is not None or snapshot.get("direction_id") is not None:
                    raise ValueError("STORY_PLAN_INVALID")
            else:
                plan = validate_plan(plan)
            draft = result["draft"]
            checked = parse_story_writer_response(json.dumps({field: draft[field] for field in
                ("title", "story", "synopsis", "characters", "scenes")}, ensure_ascii=False))
            # Repair changes only prose. Its old summaries retain their source
            # identity instead of being silently relabelled as new story facts.
            derived_hash = draft.get("derived_from_story_sha256", checked["story_sha256"])
            if not isinstance(derived_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", derived_hash):
                raise ValueError("STORY_DERIVED_SOURCE_INVALID")
            review = _current_review(result["reviews"][-1], brief, checked["story"])
        elif operation == "review":
            result = _current_review(review_story(brief, body, **options), brief, body)
        elif operation == "rewrite":
            start, end = snapshot["start"], snapshot["end"]
            result = propose_rewrite(brief, body, start, end, snapshot["instruction"], **options)
            replacement = result["replacement"]
            if not isinstance(replacement, str) or replacement == body[start:end]:
                raise ValueError("STORY_REWRITE_INVALID")
            candidate = body[:start] + replacement + body[end:]
            validate_story_text(candidate)
            if len(candidate) > 12000 or result["story"] != candidate:
                raise ValueError("STORY_REWRITE_SCOPE_INVALID")
            review = _current_review(result["review"], brief, candidate)
        else:
            raise ValueError("STORY_OPERATION_INVALID")
        with LOCK:
            record = _load(owner, identity)
            if not _task_matches(record, request_id):
                return
            if not _snapshot_matches(record, snapshot):
                raise ValueError("STORY_INPUT_CHANGED")
            if operation == "directions":
                record["development"] = {"plan": result, "status": "current", "brief_sha256": _brief_hash(brief)}
            elif operation == "develop":
                _invalidate_editorial(record)
                record.update({field: checked[field] for field in ("title", "story", "synopsis", "characters", "scenes")})
                _version(record, "developed")
                record.update(derived_from_story_sha256=derived_hash,
                              derived_status="current" if derived_hash == record["story_sha256"] else "stale",
                              character_identity_status="suggested", review=review, editorial_review=review["verdict"])
                record["development"] = {"plan": plan, "status": "current", "brief_sha256": _brief_hash(brief),
                                         "revision_rounds": result["revision_rounds"], "reviews": result["reviews"]}
            elif operation == "review":
                record.update(review=result, editorial_review=result["verdict"])
            else:
                record["rewrite_proposal"] = {
                    "id": uuid.uuid4().hex, "status": "pending", "base_story_revision": snapshot["story_revision"],
                    "base_story_sha256": story_text_sha256(body), "base_brief_sha256": _brief_hash(brief),
                    "start": start, "end": end, "original": body[start:end], "replacement": replacement,
                    "story": candidate, "instruction": snapshot["instruction"], "review": review,
                    "created_at": time.time(),
                }
            if operation in {"develop", "review"}:
                record["status"] = "needs_revision" if record["editorial_review"] == "needs_revision" else "ready"
            elif operation == "directions" and not record["story"]:
                record["status"] = "directions_ready"
            else:
                previous_status = snapshot.get("previous_status")
                record["status"] = previous_status if previous_status in {"ready", "needs_revision", "adopted"} else "ready"
            record["task"].update(status="completed", stage="completed", error="", finished_at=time.time())
            _touch(record)
            _write(owner, record)
    except Exception as exc:
        error = ("三个方向暂未选出可用稿，已保留构思记录和原稿。请调整想法或重新构思。"
                 if isinstance(exc, StoryDirectionsRejected) else
                 "本轮返回的内容或引用不符合要求，原稿已保留。可查看已完成阶段，再重新尝试。"
                 if isinstance(exc, (ValueError, KeyError, TypeError, IndexError)) else
                 "写作服务未能完成本轮请求，原稿已保留。请检查接口与服务配置后重试。")
        with LOCK:
            record = _load(owner, identity)
            if not _task_matches(record, request_id):
                return
            record["status"] = "failed"
            record["task"].update(status="failed", error=error, finished_at=time.time())
            _touch(record)
            _write(owner, record)


def _reserve_operation(owner: int, identity: str, payload: GenerateStory, operation: str) -> dict:
    signature = {"operation": operation, "parameters": payload.model_dump(exclude={"revision", "request_id"})}
    key = (owner, identity, payload.request_id)
    with LOCK:
        record = _load(owner, identity)
        previous = record.get("operation_requests", {}).get(payload.request_id)
        if previous is not None:
            if previous != signature:
                raise HTTPException(409, "这个请求标识已用于不同的故事操作，请重新开始当前操作")
            return _public(record)
        if payload.request_id in record["generation_requests"]:
            raise HTTPException(409, "这个请求标识已用于其他故事操作，请重新开始当前操作")
        _check_revision(record, payload.revision)
        _check_idle(record)
        _no_pending_rewrite(record)
        if operation in {"directions", "develop"} and record["brief"]["intent"] == "manuscript":
            raise HTTPException(409, "已有文稿可直接审阅或选区修改；重新构思请新建想法故事")
        if operation in {"review", "rewrite"}:
            try:
                validate_story_text(record["story"])
            except ValueError as exc:
                raise HTTPException(422, "请先保存完整的故事正文") from exc
        snapshot = {"brief": copy.deepcopy(record["brief"]), "story": record["story"],
                    "story_revision": record["story_revision"], "previous_status": record["status"],
                    **signature["parameters"]}
        if operation == "develop" and payload.direction_id is not None:
            development = record.get("development", {})
            if (development.get("status") != "current"
                    or development.get("brief_sha256") != _brief_hash(record["brief"])):
                raise HTTPException(409, "故事要求已变化，请重新构思方向")
            plan = validate_plan(development["plan"])
            if payload.direction_id not in {row["id"] for row in plan["directions"]}:
                raise HTTPException(422, "所选故事方向不存在")
            snapshot["plan"] = plan
        if operation == "rewrite":
            if not 0 <= payload.start < payload.end <= len(record["story"]) or not payload.instruction.strip():
                raise HTTPException(422, "请选中正文中的有效片段，并填写修改要求")
        if len(ACTIVE) >= 4 or any(item[0] == owner for item in ACTIVE):
            raise HTTPException(409, "已有写作任务进行中，请完成后再开始下一篇")
        record.setdefault("operation_requests", {})[payload.request_id] = signature
        record["status"] = "generating"
        record["task"] = {"status": "running", "operation": operation, "request_id": payload.request_id,
                          "process_id": PROCESS_ID, "started_at": time.time(), "error": "",
                          "stage": "queued", "message": "写作任务已开始。", "call_count": 0,
                          "input_snapshot": snapshot, "checkpoints": []}
        _touch(record)
        _write(owner, record)
        ACTIVE.add(key)
        try:
            _start_generation(owner, identity, payload.request_id)
        except Exception:
            ACTIVE.discard(key)
            record["status"] = "failed"
            record["task"].update(status="failed", error="写作任务未能启动，原稿已保留。请重试。", finished_at=time.time())
            _touch(record)
            _write(owner, record)
        return _public(record)


@router.post("/{identity}/directions", status_code=202)
def directions_story(identity: str, payload: GenerateStory, request: Request):
    return _reserve_operation(int(require_user(request)["id"]), identity, payload, "directions")


@router.post("/{identity}/develop", status_code=202)
def development_story(identity: str, payload: DevelopStory, request: Request):
    return _reserve_operation(int(require_user(request)["id"]), identity, payload, "develop")


@router.post("/{identity}/review", status_code=202)
def editorial_story(identity: str, payload: GenerateStory, request: Request):
    return _reserve_operation(int(require_user(request)["id"]), identity, payload, "review")


@router.post("/{identity}/rewrite", status_code=202)
def rewrite_story(identity: str, payload: RewriteStory, request: Request):
    return _reserve_operation(int(require_user(request)["id"]), identity, payload, "rewrite")


def _decide_rewrite(owner: int, identity: str, payload: RewriteDecision, *, accept: bool) -> dict:
    with LOCK:
        record = _load(owner, identity)
        _check_revision(record, payload.revision)
        _check_idle(record)
        proposal = record.get("rewrite_proposal", {})
        if proposal.get("id") != payload.proposal_id or proposal.get("status") != "pending":
            raise HTTPException(409, "修改建议已处理或已过期，请读取当前正文后重新选择")
        if accept:
            if (proposal["base_story_revision"] != record["story_revision"]
                    or proposal["base_story_sha256"] != record["story_sha256"]
                    or proposal["base_brief_sha256"] != _brief_hash(record["brief"])):
                raise HTTPException(409, "正文或创作要求已变化，这份修改建议不能再采用")
            start, end = proposal["start"], proposal["end"]
            body = record["story"]
            candidate = body[:start] + proposal["replacement"] + body[end:]
            if body[start:end] != proposal["original"] or candidate != proposal["story"]:
                raise HTTPException(409, "正文选区已变化，请重新生成修改建议")
            review = _current_review({field: proposal["review"][field] for field in ("verdict", "issues")},
                                     record["brief"], candidate)
            _invalidate_editorial(record)
            record["story"] = candidate
            _version(record, "rewrite_accepted")
            record.update(derived_status="stale" if record.get("derived_from_story_sha256") else "empty",
                          review=review, editorial_review=review["verdict"],
                          status="needs_revision" if review["verdict"] == "needs_revision" else "ready")
        proposal.update(status="accepted" if accept else "discarded", decided_at=time.time())
        _touch(record)
        _write(owner, record)
        return _public(record)


@router.post("/{identity}/rewrite/accept")
def accept_rewrite(identity: str, payload: RewriteDecision, request: Request):
    return _decide_rewrite(int(require_user(request)["id"]), identity, payload, accept=True)


@router.post("/{identity}/rewrite/discard")
def discard_rewrite(identity: str, payload: RewriteDecision, request: Request):
    return _decide_rewrite(int(require_user(request)["id"]), identity, payload, accept=False)


@router.get("/presets")
def story_presets(request: Request):
    require_user(request)
    return {"presets": list(STORY_PRESETS.values()), "default_preset": "general"}


@router.get("")
def list_stories(request: Request):
    owner = int(require_user(request)["id"])
    with LOCK:
        directory = ROOT / str(owner)
        records = [_public(_load(owner, path.stem)) for path in directory.glob("*.json")]
    return {"stories": sorted(records, key=lambda row: row["updated_at"], reverse=True)}


@router.post("")
def create_story(payload: CreateStory, request: Request):
    owner = int(require_user(request)["id"])
    brief, title, story = _fields(payload)
    if payload.intent == "manuscript" and not story.strip():
        raise HTTPException(422, "请先粘贴已有文稿，再保存")
    with LOCK:
        if _path(owner, payload.id).exists():
            record = _load(owner, payload.id)
            if record.get("create_payload") != payload.model_dump():
                raise HTTPException(409, "这个草稿已创建，请打开已保存的故事继续编辑")
            return _public(record)
        now = time.time()
        record = {"id": payload.id, "owner_id": owner, "revision": 1, "brief": brief,
                  "title": title, "story": story, "synopsis": "", "characters": [], "scenes": [],
                  "story_revision": 0, "story_sha256": story_text_sha256(story),
                  "derived_status": "empty", "derived_from_story_sha256": "",
                  "editorial_review": "not_performed",
                  "status": "ready" if story else "draft", "task": {"status": "idle", "error": ""},
                  "versions": [], "generation_requests": [], "adopted_versions": [],
                  "created_at": now, "updated_at": now, "create_payload": payload.model_dump()}
        if story:
            _version(record, "imported" if payload.intent == "manuscript" else "manual")
        _write(owner, record)
        return _public(record)


@router.get("/{identity}")
def get_story(identity: str, request: Request):
    owner = int(require_user(request)["id"])
    with LOCK:
        return _public(_load(owner, identity))


@router.put("/{identity}")
def save_story(identity: str, payload: SaveStory, request: Request):
    owner = int(require_user(request)["id"])
    brief, title, story = _fields(payload)
    with LOCK:
        record = _load(owner, identity)
        _check_revision(record, payload.revision)
        _check_idle(record)
        if record["brief"] == brief and record["title"] == title and record["story"] == story:
            return _public(record)
        changed = record["story"] != story
        brief_changed = _brief_hash(record["brief"]) != _brief_hash(brief)
        record.update(brief=brief, title=title, story=story)
        if changed or brief_changed:
            _invalidate_editorial(record, brief_changed=brief_changed)
        if changed:
            _version(record, "manual")
            record["derived_status"] = "stale" if record.get("derived_from_story_sha256") else "empty"
            # Old derived material remains tied to its old fingerprint for audit;
            # it must never be presented or adopted as current story facts.
        record["status"] = "ready" if story else "draft"
        _touch(record)
        _write(owner, record)
        return _public(record)


@router.post("/{identity}/generate", status_code=202)
def generate_story(identity: str, payload: GenerateStory, request: Request):
    owner = int(require_user(request)["id"])
    key = (owner, identity, payload.request_id)
    with LOCK:
        record = _load(owner, identity)
        if payload.request_id in record.get("operation_requests", {}):
            raise HTTPException(409, "这个请求标识已用于其他故事操作，请重新开始当前操作")
        if payload.request_id in record["generation_requests"]:
            return _public(record)
        _check_revision(record, payload.revision)
        _check_idle(record)
        _no_pending_rewrite(record)
        if record["brief"]["intent"] == "manuscript":
            raise HTTPException(409, "已有文稿会保留原文。请直接编辑保存，或新建一个想法故事")
        if len(ACTIVE) >= 4 or any(item[0] == owner for item in ACTIVE):
            raise HTTPException(409, "已有写作任务进行中，请完成后再开始下一篇")
        record["generation_requests"].append(payload.request_id)
        record["status"] = "generating"
        record["task"] = {"status": "running", "request_id": payload.request_id,
                          "process_id": PROCESS_ID, "started_at": time.time(), "error": ""}
        _touch(record)
        _write(owner, record)
        ACTIVE.add(key)
        try:
            _start_generation(owner, identity, payload.request_id)
        except Exception:
            ACTIVE.discard(key)
            record["status"] = "failed"
            record["task"].update(status="failed", error="写作任务未能启动，原稿已保留。请重试。")
            _touch(record)
            _write(owner, record)
        return _public(record)


@router.post("/{identity}/adopt")
def adopt_story(identity: str, payload: StoryRevision, request: Request):
    owner = int(require_user(request)["id"])
    with LOCK:
        record = _load(owner, identity)
        _check_revision(record, payload.revision)
        _check_idle(record)
        _no_pending_rewrite(record)
        try:
            validate_story_text(record["story"])
        except ValueError as exc:
            raise HTTPException(422, "请先保存完整的故事正文，再采用到制作草稿") from exc
        if record["story_revision"] not in record["adopted_versions"]:
            record["adopted_versions"].append(record["story_revision"])
        candidate = make_story_snapshot(identity, record["story_revision"], record["story"],
                                        record["title"], record["brief"])
        snapshots = record.setdefault("adoption_snapshots", {})
        try:
            if not isinstance(snapshots, dict):
                raise ValueError("Invalid snapshot store")
            checked = [validate_story_snapshot(item) for item in snapshots.values()]
            if any(item.get("adoption_id") != key for key, item in snapshots.items()):
                raise ValueError("Invalid adoption identity")
        except (TypeError, ValueError) as exc:
            raise HTTPException(503, "已采用版本的记录无法校验，请保留当前稿件并检查存储") from exc
        snapshot = next((item for item in checked
                         if item.get("snapshot_sha256") == candidate["snapshot_sha256"]), None)
        new_adoption = snapshot is None
        if new_adoption:
            snapshot = candidate
            snapshots[snapshot["adoption_id"]] = snapshot
        if record["status"] != "adopted" or new_adoption:
            record["status"] = "adopted"
            _touch(record)
            _write(owner, record)
        source = {"story_id": identity, "story_revision": record["story_revision"],
                  "story_sha256": record["story_sha256"], "adoption_id": snapshot["adoption_id"]}
        # No character/scene seeds: inferred summaries have not been confirmed
        # against assets. No voice, visual style, or picture-book prompts change.
        return {"record": _public(record), "source": source,
                "title": record["title"] or "未命名故事", "story": record["story"], "content_mode": "general"}


def resolve_story_source(owner: int, source: dict, script: str) -> dict:
    """Resolve an adopted immutable version before creating any media job."""
    with LOCK:
        record = _load(owner, source["story_id"])
        version = next((row for row in record["versions"]
                        if row["story_revision"] == source["story_revision"]), None)
        if (not version or source["story_revision"] not in record["adopted_versions"]
                or version["story_sha256"] != source["story_sha256"]
                or story_text_sha256(script) != source["story_sha256"]):
            raise HTTPException(409, "制作正文与采用的故事版本不一致，请重新采用故事或使用独立文稿")
        if source.get("adoption_id"):
            snapshots = record.get("adoption_snapshots", {})
            if not isinstance(snapshots, dict):
                raise HTTPException(503, "已采用版本的记录暂时无法读取")
            snapshot = snapshots.get(source["adoption_id"])
            if snapshot is None:
                raise HTTPException(409, "采用记录不存在，请重新采用故事或使用独立文稿")
        else:
            # Old records did not capture a brief. Never borrow today's brief.
            snapshot = {key: source[key] for key in ("story_id", "story_revision", "story_sha256")}
            snapshot.update(title=version["title"], story=version["story"])
        try:
            return validate_story_snapshot(snapshot, source)
        except (TypeError, ValueError) as exc:
            raise HTTPException(409, "采用版本无法校验，请重新采用故事或使用独立文稿") from exc
