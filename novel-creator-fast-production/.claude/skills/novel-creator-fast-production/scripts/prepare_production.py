#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch_state import load_active_batch
from common import atomic_write_json, load_json, safe_workspace_path, utc_timestamp, validate_workspace_layout
from production_state import assignments_for, load_production_settings, production_manifest_relative, production_root_relative, READER_AGENTS


def main() -> int:
    parser = argparse.ArgumentParser(description="Create collision-free writer assignments for the active rapid-production batch.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--force", action="store_true", help="Replace an unstarted production manifest")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        current = load_json(root / "state/current.json", required=True)
        if not isinstance(current, dict):
            raise ValueError("state/current.json must be an object")
        batch = load_active_batch(root, current)
        settings = load_production_settings(root)
        manifest_path = safe_workspace_path(root, production_manifest_relative(batch), allow_missing=True)
        existing = load_json(manifest_path, default=None)
        if existing is not None and not args.force:
            raise ValueError(f"production manifest already exists: {manifest_path.relative_to(root).as_posix()}")
        if isinstance(existing, dict):
            for item in existing.get("assignments", []):
                if isinstance(item, dict):
                    output = item.get("output")
                    if isinstance(output, str) and safe_workspace_path(root, output, allow_missing=True).exists():
                        raise ValueError("cannot replace a production manifest after a writer output exists")
        production_root = safe_workspace_path(root, production_root_relative(batch), allow_missing=True)
        raw_dir = safe_workspace_path(root, production_root_relative(batch) + "/raw", allow_missing=True)
        production_root.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        assignments = assignments_for(batch, settings["writer_pool_size"])
        record = {
            "schema": 1,
            "batch_id": batch["batch_id"],
            "start_chapter": batch["start_chapter"],
            "end_chapter": batch["end_chapter"],
            "batch_size": batch["batch_size"],
            "writer_pool_size": settings["writer_pool_size"],
            "blind_reader_count": settings["blind_reader_count"],
            "reader_agents": list(READER_AGENTS[: settings["blind_reader_count"]]),
            "created_at": utc_timestamp(),
            "assignments": assignments,
        }
        atomic_write_json(manifest_path, record)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "prepared": True,
        "batch": f"{batch['start_chapter']}-{batch['end_chapter']}",
        "manifest": manifest_path.relative_to(root).as_posix(),
        "assignments": assignments,
        "reader_agents": record["reader_agents"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
