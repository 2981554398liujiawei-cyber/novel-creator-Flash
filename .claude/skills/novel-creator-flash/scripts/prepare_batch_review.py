#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch_review import chapter_key, current_prose_hash
from batch_state import batch_review_relative, load_active_batch
from common import atomic_write_json, load_json, safe_workspace_path, utc_timestamp, validate_workspace_layout
from production_state import load_production_settings, READER_AGENTS


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze the active batch and create its structured review record.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--force", action="store_true", help="Replace an existing unfinalized review record")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        current = load_json(root / "state/current.json", required=True)
        if not isinstance(current, dict):
            raise ValueError("state/current.json must be an object")
        batch = load_active_batch(root, current)
        path = safe_workspace_path(root, batch_review_relative(batch), allow_missing=True)
        existing = load_json(path, default=None)
        if existing is not None and not args.force:
            raise ValueError(f"batch review record already exists: {path.relative_to(root).as_posix()}")
        if isinstance(existing, dict) and existing.get("finalized") is True:
            raise ValueError("finalized batch review record cannot be replaced")
        production = load_production_settings(root)
        hashes = {
            chapter_key(chapter): current_prose_hash(root, chapter)
            for chapter in range(batch["start_chapter"], batch["end_chapter"] + 1)
        }
        record = {
            "schema": 1,
            "batch_id": batch["batch_id"],
            "start_chapter": batch["start_chapter"],
            "end_chapter": batch["end_chapter"],
            "batch_size": batch["batch_size"],
            "prepared_at": utc_timestamp(),
            "frozen_hashes": hashes,
            "first_reader": {
                "status": "pending",
                "required_count": production["blind_reader_count"],
                "completed_readers": [],
                "available_readers": list(READER_AGENTS[: production["blind_reader_count"]]),
                "verdict": "",
                "ending_pull": "",
                "revision_applied": None,
                "issue_tags": [],
                "highest_value_revision": "",
            },
            "continuity": {
                "status": "pending",
                "checked_by": "main-agent",
                "blocking_count": None,
                "warning_count": None,
            },
            "finalized": False,
            "finalized_at": None,
            "final_hashes": dict(hashes),
        }
        atomic_write_json(path, record)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "prepared": True,
        "batch": f"{batch['start_chapter']}-{batch['end_chapter']}",
        "output": path.relative_to(root).as_posix(),
        "next": "Run the configured blind-reader panel, let main Claude complete continuity checking, write the aggregate conclusions, revise prose, then run finalize-review.",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
