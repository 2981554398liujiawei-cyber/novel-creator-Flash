#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import chapter_filename, load_json, read_text, safe_workspace_path, validate_workspace_layout

DEFAULT_LENGTH_SETTINGS = {
    "minimum_effective_chars": 2700,
    "target_effective_chars": 3200,
    "soft_maximum_effective_chars": 4200,
}

DEFAULT_BATCH_SETTINGS = {
    "batch_size": 5,
    "planning_window": 10,
}


def _real_int(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    return value


def load_length_settings(root: Path) -> dict[str, int]:
    """Load project defaults while remaining compatible with projects created earlier."""
    path = safe_workspace_path(root, "state/writing-settings.json", allow_missing=True)
    data = load_json(path, default=None)
    if data is None:
        return dict(DEFAULT_LENGTH_SETTINGS)
    if not isinstance(data, dict):
        raise ValueError("state/writing-settings.json must be an object")
    if data.get("schema") != 1 or isinstance(data.get("schema"), bool):
        raise ValueError("state/writing-settings.json schema must be integer 1")
    raw = data.get("chapter_length")
    if not isinstance(raw, dict):
        raise ValueError("state/writing-settings.json chapter_length must be an object")
    settings = {
        "minimum_effective_chars": _real_int(
            raw.get("minimum_effective_chars"), "chapter_length.minimum_effective_chars", minimum=1
        ),
        "target_effective_chars": _real_int(
            raw.get("target_effective_chars"), "chapter_length.target_effective_chars", minimum=1
        ),
        "soft_maximum_effective_chars": _real_int(
            raw.get("soft_maximum_effective_chars"), "chapter_length.soft_maximum_effective_chars", minimum=1
        ),
    }
    if settings["target_effective_chars"] < settings["minimum_effective_chars"]:
        raise ValueError("target_effective_chars must be at least minimum_effective_chars")
    if settings["soft_maximum_effective_chars"] < settings["target_effective_chars"]:
        raise ValueError("soft_maximum_effective_chars must be at least target_effective_chars")
    return settings


def load_batch_settings(root: Path, *, allow_legacy_batch_size: bool = False) -> dict[str, int]:
    """Load the fixed five-chapter review rhythm.

    ``allow_legacy_batch_size`` is used only by ``configure`` so an older
    project can be normalized back to five without requiring manual JSON edits.
    All production/review/commit paths fail closed on any non-five value.
    """
    path = safe_workspace_path(root, "state/writing-settings.json", allow_missing=True)
    data = load_json(path, default=None)
    if data is None:
        return dict(DEFAULT_BATCH_SETTINGS)
    if not isinstance(data, dict):
        raise ValueError("state/writing-settings.json must be an object")
    raw = data.get("batch")
    if raw is None:
        return dict(DEFAULT_BATCH_SETTINGS)
    if not isinstance(raw, dict):
        raise ValueError("state/writing-settings.json batch must be an object")
    settings = {
        "batch_size": _real_int(raw.get("batch_size"), "batch.batch_size", minimum=1),
        "planning_window": _real_int(raw.get("planning_window"), "batch.planning_window", minimum=1),
    }
    if settings["batch_size"] != 5 and not allow_legacy_batch_size:
        raise ValueError("batch.batch_size must equal the fixed review batch size 5; run configure --batch-size 5")
    if settings["planning_window"] < 5:
        raise ValueError("batch.planning_window must be at least 5")
    return settings


def resolve_length_settings(
    root: Path,
    *,
    minimum: int | None = None,
    target: int | None = None,
    soft_maximum: int | None = None,
) -> dict[str, int]:
    settings = load_length_settings(root)
    if minimum is not None:
        settings["minimum_effective_chars"] = _real_int(minimum, "--min-chars", minimum=1)
    if target is not None:
        settings["target_effective_chars"] = _real_int(target, "--target-chars", minimum=1)
    if soft_maximum is not None:
        settings["soft_maximum_effective_chars"] = _real_int(soft_maximum, "--soft-max-chars", minimum=1)
    if settings["target_effective_chars"] < settings["minimum_effective_chars"]:
        raise ValueError("target chars must be at least minimum chars")
    if settings["soft_maximum_effective_chars"] < settings["target_effective_chars"]:
        raise ValueError("soft maximum chars must be at least target chars")
    return settings


def prose_body(text: str) -> str:
    """Remove only the first Markdown chapter heading; preserve all prose beneath it."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = normalized.split("\n")
    first_nonempty = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_nonempty is not None and lines[first_nonempty].lstrip().startswith("#"):
        del lines[first_nonempty]
    return "\n".join(lines).strip()


def analyze_chapter_text(text: str, settings: dict[str, int]) -> dict[str, Any]:
    body = prose_body(text)
    effective = sum(1 for char in body if char.isalnum())
    non_whitespace = sum(1 for char in body if not char.isspace())
    paragraphs = [part for part in body.split("\n\n") if part.strip()]
    minimum = settings["minimum_effective_chars"]
    target = settings["target_effective_chars"]
    soft_maximum = settings["soft_maximum_effective_chars"]
    if effective < minimum:
        status = "too_short"
    elif effective < target:
        status = "acceptable"
    elif effective <= soft_maximum:
        status = "on_target"
    else:
        status = "above_soft_maximum"
    return {
        "effective_chars": effective,
        "non_whitespace_chars": non_whitespace,
        "paragraphs": len(paragraphs),
        "minimum_effective_chars": minimum,
        "target_effective_chars": target,
        "soft_maximum_effective_chars": soft_maximum,
        "status": status,
        "passes_minimum": effective >= minimum,
        "shortfall": max(0, minimum - effective),
        "distance_to_target": max(0, target - effective),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure deterministic chapter length. Effective chars count letters and digits in prose, excluding the heading, spaces, and punctuation."
    )
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--input", default="", help="Optional workspace-relative Markdown path")
    parser.add_argument("--min-chars", type=int)
    parser.add_argument("--target-chars", type=int)
    parser.add_argument("--soft-max-chars", type=int)
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    validate_workspace_layout(root)
    if args.chapter < 1:
        parser.error("chapter must be positive")
    if args.input:
        try:
            source = safe_workspace_path(root, args.input, allow_missing=False)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        staging = safe_workspace_path(root, f".novel/staging/{chapter_filename(args.chapter)}", allow_missing=True)
        formal = safe_workspace_path(root, f"chapters/{chapter_filename(args.chapter)}", allow_missing=True)
        source = staging if staging.is_file() else formal
    if not source.is_file():
        parser.error(f"chapter prose is missing: {source.relative_to(root)}")
    if getattr(source.stat(), "st_nlink", 1) != 1:
        parser.error(f"hard-linked chapter input is not allowed: {source.relative_to(root)}")
    try:
        settings = resolve_length_settings(
            root,
            minimum=args.min_chars,
            target=args.target_chars,
            soft_maximum=args.soft_max_chars,
        )
    except ValueError as exc:
        parser.error(str(exc))
    result = analyze_chapter_text(read_text(source, required=True), settings)
    result.update({
        "chapter": args.chapter,
        "source": source.relative_to(root).as_posix(),
        "metric": "letters_and_digits_excluding_heading_whitespace_and_punctuation",
    })
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
