# SPDX-License-Identifier: AGPL-3.0-only
"""Immutable adopted-story inputs for production, without model or media work.

Only the adopted body and its writing brief cross this boundary. Draft summaries,
suggested characters and asset names are never production facts. A current
handoff proves text provenance; it does not prove artistic or visual correctness.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from typing import Any

from .story_writer import canonical_story_brief, story_text_sha256, validate_story_text


SNAPSHOT_VERSION = 1
_IDENTITY = re.compile(r"[a-f0-9]{32}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]")
_BASE_FIELDS = {"story_id", "story_revision", "story_sha256", "title", "story"}
_SNAPSHOT_FIELDS = _BASE_FIELDS | {
    "snapshot_version", "adoption_id", "brief", "brief_sha256", "snapshot_sha256",
}
_SOURCE_FIELDS = {"story_id", "story_revision", "story_sha256", "adoption_id"}
# Deliberately explicit: do not erase all Unicode punctuation, symbols, digits,
# letter case, or wording. In particular -, /, +, %, currency and math survive.
_FORMATTING_PUNCTUATION = frozenset(".,!?;:\"'()[]{}。，、！？；：“”‘’（）【】〔〕「」『』《》〈〉…")
_NUMERIC_SEPARATORS = frozenset(".,:，：")
_WHITESPACE = frozenset(" \t\r\n\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
                        "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")


def _hash(value: Any) -> str:
    # Match story_workspace's canonical brief digest.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _brief(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("STORY_PRODUCTION_BRIEF_INVALID")
    try:
        normalized = canonical_story_brief(
            idea=value["idea"], preset=value["preset"],
            duration_seconds=value["duration_seconds"], intent=value["intent"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("STORY_PRODUCTION_BRIEF_INVALID") from exc
    if value != normalized:
        raise ValueError("STORY_PRODUCTION_BRIEF_INVALID")
    return normalized


def _snapshot_hash(snapshot: dict) -> str:
    return _hash({key: snapshot[key] for key in (
        "snapshot_version", "story_id", "story_revision", "story_sha256", "story", "brief",
    )})


def make_story_snapshot(story_id: str, story_revision: int, story: str, title: str,
                        brief: dict, *, adoption_id: str | None = None) -> dict:
    """Freeze the exact body and canonical brief for one adoption.

    Equal source versions, exact bodies and briefs share a snapshot digest even
    when display titles or generated adoption IDs differ. The caller may use the
    digest to reuse an already stored adoption. No summaries or assets are read.
    """
    normalized = _brief(brief)
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "story_id": story_id, "story_revision": story_revision,
        "story_sha256": story_text_sha256(story),
        "adoption_id": uuid.uuid4().hex if adoption_id is None else adoption_id,
        "title": title, "story": story, "brief": normalized,
        "brief_sha256": _hash(normalized),
    }
    snapshot["snapshot_sha256"] = _snapshot_hash(snapshot)
    return validate_story_snapshot(snapshot)


def validate_story_snapshot(snapshot: dict, source: dict | None = None) -> dict:
    """Return an independent checked copy; never infer missing legacy briefs.

    Hashes detect inconsistent records, not hostile authors. Ownership and
    adopted-version authorization belong to the caller's authenticated store.
    When supplied, source must identify this exact adoption (or the legacy body).
    """
    code = "STORY_PRODUCTION_SNAPSHOT_INVALID"
    if not isinstance(snapshot, dict):
        raise ValueError(code)
    legacy = "snapshot_version" not in snapshot
    if legacy:
        # New StorySource.model_dump() can add its default null adoption_id to
        # an old body-only record. This is still legacy, never a new adoption.
        allowed = _BASE_FIELDS | {"adoption_id"}
        if not _BASE_FIELDS.issubset(snapshot) or set(snapshot) - allowed or snapshot.get("adoption_id") is not None:
            raise ValueError(code)
    elif (set(snapshot) != _SNAPSHOT_FIELDS or type(snapshot["snapshot_version"]) is not int
          or snapshot["snapshot_version"] != SNAPSHOT_VERSION):
        raise ValueError(code)
    if (not _identifier(snapshot.get("story_id")) or type(snapshot.get("story_revision")) is not int
            or snapshot["story_revision"] < 1 or not _digest(snapshot.get("story_sha256"))):
        raise ValueError(code)
    title = snapshot.get("title")
    if not isinstance(title, str) or len(title) > 80 or _CONTROL.search(title):
        raise ValueError(code)
    story = snapshot.get("story")
    try:
        validate_story_text(story)
        if len(story) > 12000 or story_text_sha256(story) != snapshot["story_sha256"]:
            raise ValueError(code)
    except (TypeError, ValueError) as exc:
        raise ValueError(code) from exc
    result = copy.deepcopy(snapshot)
    if legacy:
        result.pop("adoption_id", None)
    else:
        if not _identifier(snapshot.get("adoption_id")):
            raise ValueError(code)
        result["brief"] = _brief(snapshot["brief"])
        if (not _digest(snapshot.get("brief_sha256")) or snapshot["brief_sha256"] != _hash(result["brief"])
                or not _digest(snapshot.get("snapshot_sha256"))
                or snapshot["snapshot_sha256"] != _snapshot_hash(result)):
            raise ValueError(code)
    if source is not None:
        if (not isinstance(source, dict) or set(source) - _SOURCE_FIELDS
                or not _identifier(source.get("story_id")) or type(source.get("story_revision")) is not int
                or not _digest(source.get("story_sha256"))
                or any(source.get(key) != result.get(key) for key in _SOURCE_FIELDS)):
            raise ValueError("STORY_PRODUCTION_SOURCE_MISMATCH")
    return result


def _comparison_text(text: Any) -> str | None:
    if not isinstance(text, str) or _CONTROL.search(text):
        return None
    text = "".join(character for character in text if character not in _WHITESPACE)
    result = []
    for index, character in enumerate(text):
        # Decimal values, thousands separators and clock times cannot silently
        # collapse to a different numeric token (e.g. 1.5 must not equal 15).
        numeric = (character in _NUMERIC_SEPARATORS and 0 < index < len(text) - 1
                   and text[index - 1].isdigit() and text[index + 1].isdigit())
        if character not in _FORMATTING_PUNCTUATION or numeric:
            result.append(character)
    return "".join(result)


def _scene_text(scenes: Any) -> str | None:
    if not isinstance(scenes, list) or not scenes:
        return None
    texts = []
    for scene in scenes:
        if not isinstance(scene, dict):
            return None
        value = scene.get("text", scene.get("text_content"))
        if not isinstance(value, str):
            return None
        if "text" in scene and "text_content" in scene and scene["text"] != scene["text_content"]:
            return None
        texts.append(value)
    return _comparison_text("\n".join(texts))


def production_story_handoff(snapshot: dict, scenes: list[dict]) -> dict:
    """Attach frozen writing inputs only while production text still matches.

    Subtitle splitting and explicit punctuation/whitespace formatting may vary.
    No fuzzy match, spelling correction, numeric conversion or dropped negation
    can retain the adopted brief. Detached records expose provenance labels only.
    """
    checked = validate_story_snapshot(snapshot)
    source = {key: checked[key] for key in ("story_id", "story_revision", "story_sha256")}
    if "adoption_id" in checked:
        source["adoption_id"] = checked["adoption_id"]
    result = {"source": source, "title": checked["title"]}
    if "snapshot_sha256" in checked:
        result["snapshot_sha256"] = checked["snapshot_sha256"]
    current = _scene_text(scenes) == _comparison_text(checked["story"])
    if not current:
        result.update(status="detached", message="制作正文已改变；保留来源记录，本次不沿用采用时的创作要求。")
    elif "snapshot_version" not in checked:
        result.update(status="legacy_body_only", message="正文与采用版本一致；旧记录未保存当时的创作要求。")
    else:
        result.update(status="current", message="正文与采用版本一致，制作将沿用采用时的创作要求。",
                      story=checked["story"], brief=checked["brief"], brief_sha256=checked["brief_sha256"])
    return result
