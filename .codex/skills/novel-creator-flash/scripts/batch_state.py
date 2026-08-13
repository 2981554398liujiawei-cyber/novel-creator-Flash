#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from chapter_stats import load_batch_settings
from common import load_json, safe_workspace_path

REVIEW_BATCH_SIZE = 5
FINAL_TAIL_MIN = 1
FINAL_TAIL_MAX = REVIEW_BATCH_SIZE - 1

BATCH_FIELDS = (
    "batch_id",
    "start_chapter",
    "end_chapter",
    "batch_size",
    "next_review_chapter",
)


def _real_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def make_batch(*, batch_id: int, start_chapter: int, batch_size: int = REVIEW_BATCH_SIZE) -> dict[str, int]:
    batch_id = _real_int(batch_id, "batch.batch_id")
    start_chapter = _real_int(start_chapter, "batch.start_chapter")
    batch_size = _real_int(batch_size, "batch.batch_size")
    if batch_size != REVIEW_BATCH_SIZE:
        raise ValueError(f"Novel Creator review batches are permanently fixed at {REVIEW_BATCH_SIZE} chapters")
    end_chapter = start_chapter + REVIEW_BATCH_SIZE - 1
    return {
        "batch_id": batch_id,
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
        "batch_size": REVIEW_BATCH_SIZE,
        "next_review_chapter": end_chapter,
    }


def validate_batch(value: Any, *, field: str = "batch") -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    errors: list[str] = []
    parsed: dict[str, int] = {}
    for key in BATCH_FIELDS:
        try:
            parsed[key] = _real_int(value.get(key), f"{field}.{key}")
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        return errors
    if parsed["batch_size"] != REVIEW_BATCH_SIZE:
        errors.append(f"{field}.batch_size must equal fixed review batch size {REVIEW_BATCH_SIZE}")
    expected_end = parsed["start_chapter"] + REVIEW_BATCH_SIZE - 1
    if parsed["end_chapter"] != expected_end:
        errors.append(f"{field}.end_chapter must equal start_chapter + {REVIEW_BATCH_SIZE - 1}")
    if parsed["next_review_chapter"] != parsed["end_chapter"]:
        errors.append(f"{field}.next_review_chapter must equal end_chapter")
    return errors


def infer_batch(current: dict[str, Any], settings: dict[str, int]) -> dict[str, int]:
    latest = current.get("latest_chapter", 0)
    if not isinstance(latest, int) or isinstance(latest, bool) or latest < 0:
        raise ValueError("state/current.json latest_chapter must be a non-negative integer")
    if settings.get("batch_size") != REVIEW_BATCH_SIZE:
        raise ValueError(f"project batch_size must be {REVIEW_BATCH_SIZE}; run configure --batch-size {REVIEW_BATCH_SIZE}")
    return make_batch(batch_id=1, start_chapter=latest + 1)


def load_active_batch(root: Path, current: dict[str, Any] | None = None) -> dict[str, int]:
    if current is None:
        current_path = safe_workspace_path(root, "state/current.json", allow_missing=False)
        current_data = load_json(current_path, required=True)
        if not isinstance(current_data, dict):
            raise ValueError("state/current.json must be an object")
        current = current_data
    raw = current.get("batch")
    if raw is None:
        return infer_batch(current, load_batch_settings(root))
    errors = validate_batch(raw, field="state/current.json batch")
    if errors:
        raise ValueError("; ".join(errors))
    return {key: int(raw[key]) for key in BATCH_FIELDS}


def advance_batch(current_batch: dict[str, int], *, next_batch_size: int = REVIEW_BATCH_SIZE) -> dict[str, int]:
    if next_batch_size != REVIEW_BATCH_SIZE:
        raise ValueError(f"next review batch size must remain fixed at {REVIEW_BATCH_SIZE}")
    return make_batch(
        batch_id=current_batch["batch_id"] + 1,
        start_chapter=current_batch["end_chapter"] + 1,
    )


def make_next_batch_after_chapter(current_batch: dict[str, int], chapter: int) -> dict[str, int]:
    chapter = _real_int(chapter, "chapter")
    return make_batch(batch_id=current_batch["batch_id"] + 1, start_chapter=chapter + 1)


def make_final_tail(active_batch: dict[str, int], chapter_count: int) -> dict[str, Any]:
    chapter_count = _real_int(chapter_count, "final_tail.chapter_count")
    if chapter_count > FINAL_TAIL_MAX:
        raise ValueError(f"final tail must contain between {FINAL_TAIL_MIN} and {FINAL_TAIL_MAX} chapters")
    start = active_batch["start_chapter"]
    end = start + chapter_count - 1
    return {
        "schema": 1,
        "review_kind": "final_tail",
        "batch_id": active_batch["batch_id"],
        "start_chapter": start,
        "end_chapter": end,
        "batch_size": chapter_count,
        "next_review_chapter": end,
        "chapter_count": chapter_count,
        "writer": "main-agent",
    }


def validate_final_tail(value: Any, active_batch: dict[str, int], *, field: str = "final_tail") -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    errors: list[str] = []
    if value.get("schema") != 1 or isinstance(value.get("schema"), bool):
        errors.append(f"{field}.schema must be integer 1")
    if value.get("review_kind") != "final_tail":
        errors.append(f"{field}.review_kind must be final_tail")
    try:
        count = _real_int(value.get("chapter_count"), f"{field}.chapter_count")
        start = _real_int(value.get("start_chapter"), f"{field}.start_chapter")
        end = _real_int(value.get("end_chapter"), f"{field}.end_chapter")
        batch_id = _real_int(value.get("batch_id"), f"{field}.batch_id")
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    if count > FINAL_TAIL_MAX:
        errors.append(f"{field}.chapter_count must be at most {FINAL_TAIL_MAX}")
    if start != active_batch["start_chapter"]:
        errors.append(f"{field}.start_chapter must equal active batch start {active_batch['start_chapter']}")
    if batch_id != active_batch["batch_id"]:
        errors.append(f"{field}.batch_id must equal active batch id {active_batch['batch_id']}")
    if end != start + count - 1:
        errors.append(f"{field}.end_chapter must equal start_chapter + chapter_count - 1")
    if value.get("writer") != "main-agent":
        errors.append(f"{field}.writer must be main-agent")
    return errors


def load_active_review_unit(root: Path, current: dict[str, Any] | None = None) -> dict[str, Any]:
    if current is None:
        current_path = safe_workspace_path(root, "state/current.json", allow_missing=False)
        current_data = load_json(current_path, required=True)
        if not isinstance(current_data, dict):
            raise ValueError("state/current.json must be an object")
        current = current_data
    active_batch = load_active_batch(root, current)
    tail = current.get("final_tail")
    if tail is None:
        return {**active_batch, "review_kind": "batch"}
    errors = validate_final_tail(tail, active_batch, field="state/current.json final_tail")
    if errors:
        raise ValueError("; ".join(errors))
    return {
        "review_kind": "final_tail",
        "batch_id": int(tail["batch_id"]),
        "start_chapter": int(tail["start_chapter"]),
        "end_chapter": int(tail["end_chapter"]),
        "batch_size": int(tail["chapter_count"]),
        "next_review_chapter": int(tail["end_chapter"]),
    }


def validate_review_unit(value: Any, *, field: str = "review_unit") -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    kind = value.get("review_kind", "batch")
    if kind == "batch":
        return validate_batch(value, field=field)
    if kind != "final_tail":
        return [f"{field}.review_kind must be batch or final_tail"]
    errors: list[str] = []
    try:
        size = _real_int(value.get("batch_size"), f"{field}.batch_size")
        start = _real_int(value.get("start_chapter"), f"{field}.start_chapter")
        end = _real_int(value.get("end_chapter"), f"{field}.end_chapter")
        _real_int(value.get("batch_id"), f"{field}.batch_id")
        next_review = _real_int(value.get("next_review_chapter"), f"{field}.next_review_chapter")
    except ValueError as exc:
        return [str(exc)]
    if size > FINAL_TAIL_MAX:
        errors.append(f"{field}.batch_size must be between 1 and {FINAL_TAIL_MAX} for final_tail")
    if end != start + size - 1:
        errors.append(f"{field}.end_chapter must equal start_chapter + batch_size - 1")
    if next_review != end:
        errors.append(f"{field}.next_review_chapter must equal end_chapter")
    return errors


def batch_review_filename(batch: dict[str, int]) -> str:
    return f"batch-{batch['start_chapter']:04d}-{batch['end_chapter']:04d}.json"


def batch_review_relative(batch: dict[str, int]) -> str:
    return f"state/reviews/{batch_review_filename(batch)}"


def review_record_relative(unit: dict[str, Any]) -> str:
    if unit.get("review_kind") == "final_tail":
        return f"state/reviews/final-tail-{unit['start_chapter']:04d}-{unit['end_chapter']:04d}.json"
    return batch_review_relative(unit)


def chapter_in_batch(chapter: int, batch: dict[str, int]) -> bool:
    return batch["start_chapter"] <= chapter <= batch["end_chapter"]


def chapter_in_review_unit(chapter: int, unit: dict[str, Any]) -> bool:
    return unit["start_chapter"] <= chapter <= unit["end_chapter"]


def is_batch_end(chapter: int, batch: dict[str, int]) -> bool:
    return chapter == batch["end_chapter"]


def is_review_unit_end(chapter: int, unit: dict[str, Any]) -> bool:
    return chapter == unit["end_chapter"]
