#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from batch_state import batch_review_relative
from common import load_json, read_text, safe_workspace_path, sha256_text
from production_state import READER_AGENTS

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def chapter_key(number: int) -> str:
    return f"chapter-{number:04d}"


def validate_batch_review_record(data: Any, batch: dict[str, int], *, require_finalized: bool = False) -> list[str]:
    if not isinstance(data, dict):
        return ["batch review record must be an object"]
    errors: list[str] = []
    if data.get("schema") != 1 or isinstance(data.get("schema"), bool):
        errors.append("batch review schema must be integer 1")
    expected = {
        "batch_id": batch["batch_id"],
        "start_chapter": batch["start_chapter"],
        "end_chapter": batch["end_chapter"],
        "batch_size": batch["batch_size"],
    }
    for key, expected_value in expected.items():
        if data.get(key) != expected_value:
            errors.append(f"batch review {key} must equal {expected_value}")
    for field in ("frozen_hashes", "final_hashes"):
        value = data.get(field)
        if not isinstance(value, dict):
            errors.append(f"batch review {field} must be an object")
            continue
        expected_keys = {chapter_key(number) for number in range(batch["start_chapter"], batch["end_chapter"] + 1)}
        actual_keys = set(value)
        if actual_keys != expected_keys:
            errors.append(f"batch review {field} chapter keys do not match active batch")
        for key, digest in value.items():
            if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
                errors.append(f"batch review {field}.{key} must be a SHA-256 hex string")
    first_reader = data.get("first_reader")
    if not isinstance(first_reader, dict):
        errors.append("batch review first_reader must be an object")
    else:
        if first_reader.get("status") not in {"pending", "completed"}:
            errors.append("batch review first_reader.status must be pending or completed")
        required_count = first_reader.get("required_count")
        if not isinstance(required_count, int) or isinstance(required_count, bool) or required_count < 2 or required_count > 3:
            errors.append("batch review first_reader.required_count must be an integer between 2 and 3")
        available_readers = first_reader.get("available_readers", [])
        if not isinstance(available_readers, list) or any(item not in READER_AGENTS for item in available_readers):
            errors.append("batch review first_reader.available_readers contains an unknown reader agent")
            available_readers = []
        completed_readers = first_reader.get("completed_readers", [])
        if not isinstance(completed_readers, list) or any(not isinstance(item, str) or not item.strip() for item in completed_readers):
            errors.append("batch review first_reader.completed_readers must be a list of non-empty strings")
            completed_readers = []
        elif len(set(completed_readers)) != len(completed_readers):
            errors.append("batch review first_reader.completed_readers must be unique")
        elif any(item not in available_readers for item in completed_readers):
            errors.append("batch review completed_readers must come from available_readers")
        if first_reader.get("status") == "completed":
            if isinstance(required_count, int) and len(completed_readers) < required_count:
                errors.append("batch review reader panel has fewer completed readers than required")
            if first_reader.get("verdict") not in {"strong", "acceptable", "weak"}:
                errors.append("batch review first_reader.verdict is invalid")
            if first_reader.get("ending_pull") not in {"strong", "fair", "weak"}:
                errors.append("batch review first_reader.ending_pull is invalid")
            if not isinstance(first_reader.get("revision_applied"), bool):
                errors.append("batch review first_reader.revision_applied must be boolean")
            tags = first_reader.get("issue_tags", [])
            if not isinstance(tags, list) or len(tags) > 3 or any(
                not isinstance(item, str) or not item.strip() or len(item.strip()) > 80
                for item in tags
            ):
                errors.append("batch review first_reader.issue_tags must contain at most 3 non-empty strings of at most 80 characters")
            if not isinstance(first_reader.get("highest_value_revision", ""), str):
                errors.append("batch review first_reader.highest_value_revision must be a string")
    continuity = data.get("continuity")
    if not isinstance(continuity, dict):
        errors.append("batch review continuity must be an object")
    else:
        if continuity.get("status") not in {"pending", "completed"}:
            errors.append("batch review continuity.status must be pending or completed")
        if continuity.get("status") == "completed":
            blocking = continuity.get("blocking_count")
            warning_count = continuity.get("warning_count")
            if not isinstance(blocking, int) or isinstance(blocking, bool) or blocking < 0:
                errors.append("batch review continuity.blocking_count must be a non-negative integer")
            if not isinstance(warning_count, int) or isinstance(warning_count, bool) or warning_count < 0:
                errors.append("batch review continuity.warning_count must be a non-negative integer")
            if continuity.get("checked_by") != "main-agent":
                errors.append("batch review continuity.checked_by must be main-agent")
    finalized = data.get("finalized")
    if not isinstance(finalized, bool):
        errors.append("batch review finalized must be boolean")
    if require_finalized:
        if finalized is not True:
            errors.append("batch review must be finalized before committing the batch")
        if isinstance(first_reader, dict) and first_reader.get("status") != "completed":
            errors.append("blind reader panel must be completed before committing the batch")
        if isinstance(continuity, dict):
            if continuity.get("status") != "completed":
                errors.append("main Claude continuity check must be completed before committing the batch")
            if continuity.get("blocking_count") != 0:
                errors.append("main Claude continuity check must have zero blocking issues before committing the batch")
    return errors


def load_review_record(root: Path, batch: dict[str, int]) -> tuple[Path, dict[str, Any] | None]:
    path = safe_workspace_path(root, batch_review_relative(batch), allow_missing=True)
    data = load_json(path, default=None)
    return path, data if isinstance(data, dict) else data


def current_prose_hash(root: Path, chapter: int) -> str:
    formal = safe_workspace_path(root, f"chapters/chapter-{chapter:04d}.md", allow_missing=True)
    staging = safe_workspace_path(root, f".novel/staging/chapter-{chapter:04d}.md", allow_missing=True)
    path = formal if formal.is_file() else staging
    if not path.is_file():
        raise ValueError(f"batch review chapter file is missing: chapter-{chapter:04d}.md")
    return sha256_text(read_text(path, required=True).rstrip() + "\n")


def verify_final_hashes(root: Path, data: dict[str, Any], batch: dict[str, int]) -> list[str]:
    errors: list[str] = []
    final_hashes = data.get("final_hashes", {})
    if not isinstance(final_hashes, dict):
        return ["batch review final_hashes must be an object"]
    for chapter in range(batch["start_chapter"], batch["end_chapter"] + 1):
        key = chapter_key(chapter)
        try:
            actual = current_prose_hash(root, chapter)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if final_hashes.get(key) != actual:
            errors.append(f"batch review final hash no longer matches {key}; rerun finalize-review after edits")
    return errors
