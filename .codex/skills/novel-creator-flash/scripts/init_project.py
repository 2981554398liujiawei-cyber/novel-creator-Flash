#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from common import atomic_write_json, atomic_write_text, load_json, utc_timestamp, validate_workspace_layout


def rendered(path: Path, replacements: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize a clean, generic novel workspace.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--title", default="待定")
    parser.add_argument("--genre", default="待定")
    parser.add_argument("--planned-chapters", default="开放")
    parser.add_argument("--target-words", default="开放")
    parser.add_argument("--chapter-min-chars", type=int, default=2700)
    parser.add_argument("--chapter-target-chars", type=int, default=3200)
    parser.add_argument("--chapter-soft-max-chars", type=int, default=4200)
    parser.add_argument("--batch-size", type=int, default=5, help="Compatibility option; Novel Creator review batches are permanently fixed at 5")
    parser.add_argument("--planning-window", type=int, default=10)
    parser.add_argument("--writer-pool-size", type=int, default=5)
    parser.add_argument("--blind-reader-count", type=int, default=1)
    parser.add_argument("--lookahead-blocks", type=int, default=2)
    args = parser.parse_args()

    if args.chapter_min_chars < 1:
        parser.error("--chapter-min-chars must be positive")
    if args.chapter_target_chars < args.chapter_min_chars:
        parser.error("--chapter-target-chars must be at least --chapter-min-chars")
    if args.chapter_soft_max_chars < args.chapter_target_chars:
        parser.error("--chapter-soft-max-chars must be at least --chapter-target-chars")
    if args.batch_size != 5:
        parser.error("--batch-size is fixed at 5; final 1-4 chapter tails are written by 主Agent and are not configurable batches")
    if args.planning_window < 5:
        parser.error("--planning-window must be at least 5")
    if not 1 <= args.writer_pool_size <= 10:
        parser.error("--writer-pool-size must be between 1 and 10")
    if not 1 <= args.blind_reader_count <= 3:
        parser.error("--blind-reader-count must be between 1 and 3")
    if not 1 <= args.lookahead_blocks <= 10:
        parser.error("--lookahead-blocks must be between 1 and 10")

    root = Path(args.workspace).resolve()
    root.mkdir(parents=True, exist_ok=True)
    managed = ["project.md", "canon", "plot", "state", "drafts", "chapters", "revisions", "audits", "exports", ".novel", ".novel-init.json"]
    conflicts = [name for name in managed if (root / name).exists()]
    if conflicts:
        parser.error("workspace already contains novel project data: " + ", ".join(conflicts))

    assets = Path(__file__).resolve().parent.parent / "assets"
    replacements = {
        "TITLE": str(args.title),
        "GENRE": str(args.genre),
        "PLANNED_CHAPTERS": str(args.planned_chapters),
        "TARGET_WORDS": str(args.target_words),
        "CHAPTER": "1",
    }
    files = {
        "project.md": "project-template.md",
        "canon/policy.md": "canon-policy-template.md",
        "canon/names.json": "names-template.json",
        "canon/facts.md": "facts-template.md",
        "canon/changes.md": "changes-template.md",
        "canon/source-index.md": "source-index-template.md",
        "plot/master-outline.md": "master-outline-template.md",
        "plot/current-arc.md": "current-arc-template.md",
        "state/current.json": "current-state-template.json",
        "state/deltas/chapter-0001.json": "chapter-delta-template.json",
        "state/session-handoff.md": "session-handoff-template.md",
        "state/creative-lessons.md": "creative-lessons-template.md",
        "state/reader-model.json": "reader-model-template.json",
        "canon/prose-contract.md": "prose-contract-template.md",
    }
    directories = [
        ".novel/backups",
        ".novel/cleanup-pending",
        ".novel/staging",
        ".novel/staging/deltas",
        ".novel/production",
        "state/baselines",
        "state/entities/characters",
        "state/entities/locations",
        "state/entities/items",
        "state/entities/quests",
        "state/entities/foreshadows",
        "state/entities/relationships",
        "state/events",
        "state/chapters",
        "state/arc-summaries",
        "state/context",
        "state/reviews",
        "drafts",
        "chapters",
        "revisions",
        "audits",
        "exports",
    ]
    transaction_id = f"init-{uuid.uuid4().hex[:12]}"
    marker = root / ".novel-init.json"
    created: list[str] = []
    atomic_write_json(marker, {
        "schema": 1,
        "transaction_id": transaction_id,
        "status": "applying",
        "created_at": utc_timestamp(),
        "created": created,
    })
    try:
        for directory in directories:
            path = root / directory
            path.mkdir(parents=True, exist_ok=False)
            created.append(directory + "/")
        for relative, template in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, rendered(assets / template, replacements))
            created.append(relative)
        writing_settings = root / "state/writing-settings.json"
        settings_template = load_json(assets / "writing-settings-template.json", required=True)
        if not isinstance(settings_template, dict):
            raise ValueError("writing-settings-template.json must be an object")
        settings_template["chapter_length"] = {
            "minimum_effective_chars": args.chapter_min_chars,
            "target_effective_chars": args.chapter_target_chars,
            "soft_maximum_effective_chars": args.chapter_soft_max_chars,
        }
        settings_template["batch"] = {
            "batch_size": 5,
            "planning_window": args.planning_window,
        }
        settings_template["production"] = {
            "writer_pool_size": args.writer_pool_size,
            "blind_reader_count": args.blind_reader_count,
            "speculative_lookahead_blocks": args.lookahead_blocks,
        }
        atomic_write_json(writing_settings, settings_template)
        created.append("state/writing-settings.json")
        current_path = root / "state/current.json"
        current_state = load_json(current_path, required=True)
        if not isinstance(current_state, dict):
            raise ValueError("current-state-template.json must be an object")
        current_state["batch"] = {
            "batch_id": 1,
            "start_chapter": 1,
            "end_chapter": 5,
            "batch_size": 5,
            "next_review_chapter": 5,
        }
        atomic_write_json(current_path, current_state)
        events = root / "state/events/events.jsonl"
        atomic_write_text(events, "")
        created.append("state/events/events.jsonl")
        validate_workspace_layout(root)
        marker.unlink()
    except Exception:
        # Remove only entries created by this invocation, in reverse order. Directory
        # removal is non-recursive, so a concurrently inserted link or foreign file is
        # never followed or deleted outside the project.
        for relative in reversed(created):
            path = root / relative.rstrip("/")
            try:
                if relative.endswith("/"):
                    path.rmdir()
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        marker.unlink(missing_ok=True)
        raise

    print(json.dumps({
        "ready": True,
        "workspace": str(root),
        "next": "根据任务量和写手池运行 prepare-production；主Agent先规划 writer_count × 5 章骨架，再并行派发每席连续五章任务卡。",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
