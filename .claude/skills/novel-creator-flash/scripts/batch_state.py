#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from chapter_stats import load_batch_settings
from common import load_json, safe_workspace_path

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


def make_batch(*, batch_id: int, start_chapter: int, batch_size: int) -> dict[str, int]:
    batch_id = _real_int(batch_id, "batch.batch_id")
    start_chapter = _real_int(start_chapter, "batch.start_chapter")
    batch_size = _real_int(batch_size, "batch.batch_size")
    end_chapter = start_chapter + batch_size - 1
    return {
        "batch_id": batch_id,
        "start_chapter": start_chapter,
        "end_chapter": end_chapter,
        "batch_size": batch_size,
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
    expected_end = parsed["start_chapter"] + parsed["batch_size"] - 1
    if parsed["end_chapter"] != expected_end:
        errors.append(f"{field}.end_chapter must equal start_chapter + batch_size - 1")
    if parsed["next_review_chapter"] != parsed["end_chapter"]:
        errors.append(f"{field}.next_review_chapter must equal end_chapter")
    return errors


def infer_batch(current: dict[str, Any], settings: dict[str, int]) -> dict[str, int]:
    latest = current.get("latest_chapter", 0)
    if not isinstance(latest, int) or isinstance(latest, bool) or latest < 0:
        raise ValueError("state/current.json latest_chapter must be a non-negative integer")
    size = settings["batch_size"]
    # Older projects had no explicit batch anchor. Start a fresh batch at the next
    # uncommitted chapter rather than retroactively applying absolute modulo rules.
    return make_batch(batch_id=1, start_chapter=latest + 1, batch_size=size)


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


def advance_batch(current_batch: dict[str, int], *, next_batch_size: int) -> dict[str, int]:
    return make_batch(
        batch_id=current_batch["batch_id"] + 1,
        start_chapter=current_batch["end_chapter"] + 1,
        batch_size=next_batch_size,
    )


def batch_review_filename(batch: dict[str, int]) -> str:
    return f"batch-{batch['start_chapter']:04d}-{batch['end_chapter']:04d}.json"


def batch_review_relative(batch: dict[str, int]) -> str:
    return f"state/reviews/{batch_review_filename(batch)}"


def chapter_in_batch(chapter: int, batch: dict[str, int]) -> bool:
    return batch["start_chapter"] <= chapter <= batch["end_chapter"]


def is_batch_end(chapter: int, batch: dict[str, int]) -> bool:
    return chapter == batch["end_chapter"]
