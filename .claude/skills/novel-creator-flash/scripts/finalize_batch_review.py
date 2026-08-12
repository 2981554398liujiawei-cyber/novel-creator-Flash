#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch_review import chapter_key, current_prose_hash, load_review_record, validate_batch_review_record
from batch_state import load_active_review_unit
from blind_packet import validate_blind_packet_record
from common import atomic_write_json, load_json, utc_timestamp, validate_workspace_layout


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate both batch reviews and bind them to the revised prose hashes.")
    parser.add_argument("workspace", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        current = load_json(root / "state/current.json", required=True)
        if not isinstance(current, dict):
            raise ValueError("state/current.json must be an object")
        batch = load_active_review_unit(root, current)
        path, record = load_review_record(root, batch)
        if not isinstance(record, dict):
            raise ValueError(f"batch review record is missing: {path.relative_to(root).as_posix()}")
        pre_errors = validate_batch_review_record(record, batch, require_finalized=False)
        pre_errors.extend(validate_blind_packet_record(root, record.get("blind_packet")))
        if pre_errors:
            raise ValueError("; ".join(pre_errors))
        if record.get("first_reader", {}).get("status") != "completed":
            raise ValueError("blind reader panel is not completed")
        if record.get("continuity", {}).get("status") != "completed":
            raise ValueError("连续性检查尚未完成")
        if record.get("continuity", {}).get("blocking_count") != 0:
            raise ValueError("连续性检查仍有 blocking 问题")
        record["final_hashes"] = {
            chapter_key(chapter): current_prose_hash(root, chapter)
            for chapter in range(batch["start_chapter"], batch["end_chapter"] + 1)
        }
        record["finalized"] = True
        record["finalized_at"] = utc_timestamp()
        post_errors = validate_batch_review_record(record, batch, require_finalized=True)
        if post_errors:
            raise ValueError("; ".join(post_errors))
        atomic_write_json(path, record)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "finalized": True,
        "review_kind": batch.get("review_kind", "batch"),
        "range": f"{batch['start_chapter']}-{batch['end_chapter']}",
        "output": path.relative_to(root).as_posix(),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
