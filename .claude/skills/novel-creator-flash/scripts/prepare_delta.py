#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from batch_state import is_review_unit_end, load_active_review_unit

from common import (
    atomic_write_json,
    chapter_filename,
    chapter_meta_filename,
    first_heading,
    load_json,
    read_text,
    safe_workspace_path,
    title_chapter_number,
    validate_workspace_layout,
)


def _blank(value: Any) -> bool:
    if value in (None, "", [], {}):
        return True
    if isinstance(value, dict):
        return all(_blank(item) for item in value.values())
    if isinstance(value, list):
        return all(_blank(item) for item in value)
    return False


def _safe_to_replace_existing(data: Any, chapter: int) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("schema") != 1 or data.get("chapter") != chapter:
        return False
    material = {
        key: data.get(key)
        for key in (
            "title", "summary", "chapter_function", "dominant_change",
            "reader_expectation_added", "entities", "events", "depends_on_events",
            "knowledge_used", "state_used", "entity_changes", "current_patch",
        )
    }
    return _blank(material)


def _title_from_heading(heading: str, chapter: int) -> str:
    clean = heading.lstrip("#").strip()
    match = re.match(r"第\s*[零〇一二两三四五六七八九十百千万\d]+\s*章\s*(.*)$", clean)
    if match:
        return match.group(1).strip() or f"第{chapter}章"
    match = re.match(r"Chapter\s+\d+\s*(.*)$", clean, re.I)
    if match:
        return match.group(1).strip() or f"Chapter {chapter}"
    return clean


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a minimal chapter delta scaffold from the current staging prose.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--force", action="store_true", help="Replace an existing non-empty delta after explicit review")
    parser.add_argument("--reader-review-reason", choices=("first-chapter", "milestone", "manual", "periodic"))
    args = parser.parse_args()

    if args.chapter < 1:
        parser.error("chapter must be positive")
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
    except ValueError as exc:
        parser.error(str(exc))

    current = load_json(root / "state/current.json", required=True)
    if not isinstance(current, dict):
        parser.error("state/current.json must be an object")
    latest = current.get("latest_chapter", 0)
    if not isinstance(latest, int) or isinstance(latest, bool) or latest < 0:
        parser.error("state/current.json latest_chapter must be a non-negative integer")
    if args.chapter != latest + 1:
        parser.error(f"prepare the next sequential chapter only; expected {latest + 1}, got {args.chapter}")

    prose_path = safe_workspace_path(root, f".novel/staging/{chapter_filename(args.chapter)}", allow_missing=False)
    prose = read_text(prose_path, required=True)
    heading = first_heading(prose)
    detected = title_chapter_number(prose)
    if detected != args.chapter or not heading:
        parser.error(f"staging prose must begin with a chapter {args.chapter} Markdown heading")

    delta_path = safe_workspace_path(root, f"state/deltas/{chapter_meta_filename(args.chapter)}", allow_missing=True)
    existing = load_json(delta_path, default=None)
    if existing is not None and not args.force and not _safe_to_replace_existing(existing, args.chapter):
        parser.error(f"delta already contains work; use --force only after reviewing it: {delta_path.relative_to(root).as_posix()}")

    try:
        active_batch = load_active_review_unit(root, current)
    except ValueError as exc:
        parser.error(str(exc))
    if not (active_batch["start_chapter"] <= args.chapter <= active_batch["end_chapter"]):
        parser.error(
            f"chapter {args.chapter} is outside active batch "
            f"{active_batch['start_chapter']}-{active_batch['end_chapter']}"
        )
    scene_bridge = {
        "time": "",
        "location": "",
        "pov": "",
        "last_action": "",
        "immediate_pressure": "",
        "emotional_residue": "",
    }
    delta = {
        "schema": 1,
        "chapter": args.chapter,
        "title": _title_from_heading(heading, args.chapter),
        "summary": "",
        "outline_node": str(current.get("outline_node", "")),
        "chapter_function": "",
        "dominant_change": "",
        "reader_expectation_added": "",
        "entities": [],
        "events": [],
        "depends_on_events": [],
        "knowledge_used": {},
        "state_used": [],
        "entity_changes": [],
        "current_patch": {
            "current_location": str(current.get("current_location", "")),
            "point_of_view": str(current.get("point_of_view", "")),
            "scene_entities": list(current.get("scene_entities", [])) if isinstance(current.get("scene_entities", []), list) else [],
            "current_goal": str(current.get("current_goal", "")),
            "scene_bridge": scene_bridge,
        },
    }
    batch_end = is_review_unit_end(args.chapter, active_batch)
    if batch_end:
        delta["current_patch"]["reader_review"] = {
            "reviewed_through_chapter": args.chapter,
            "reason": "batch",
            "verdict": "",
            "ending_pull": "",
            "revision_applied": None,
            "issue_tags": [],
            "highest_value_revision": "",
            "batch_id": active_batch["batch_id"],
            "batch_start_chapter": active_batch["start_chapter"],
            "batch_end_chapter": active_batch["end_chapter"],
            "review_kind": active_batch.get("review_kind", "batch"),
        }
    elif args.reader_review_reason:
        delta["current_patch"]["reader_review"] = {
            "reviewed_through_chapter": args.chapter,
            "reason": args.reader_review_reason,
            "verdict": "",
            "ending_pull": "",
            "revision_applied": None,
            "issue_tags": [],
            "highest_value_revision": "",
        }
    atomic_write_json(delta_path, delta)
    print(json.dumps({
        "prepared": True,
        "chapter": args.chapter,
        "title": delta["title"],
        "output": delta_path.relative_to(root).as_posix(),
        "previous_scene_bridge": current.get("scene_bridge", {}),
        "review_before_commit": [
            "本章主要承担推进、深化或蓄势中的哪一种功能？",
            "读完后，局势、关系、认知、压力或期待中，至少哪一项变得更具体？",
            "下一章为什么不能只是重复本章？",
        ],
        "active_review_unit": active_batch,
        "review_unit_end": batch_end,
        # Compatibility aliases for older integrations. Values now describe the
        # active review unit, which may be a 1-4 chapter final_tail.
        "active_batch": active_batch,
        "batch_end": batch_end,
        "reader_review_required": batch_end or bool(args.reader_review_reason),
        "note": "脚手架复制上一章地点、视角、场景人物和目标作为待复核值；summary、章节功能标签、真实变化和新的 scene_bridge 仍由主Agent 根据正文裁决。正式五章批次末章或 final-tail 末章还必须填写盲读结论。",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
