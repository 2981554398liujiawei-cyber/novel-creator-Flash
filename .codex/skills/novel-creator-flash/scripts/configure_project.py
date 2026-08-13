#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch_state import batch_review_relative, load_active_batch, make_batch
from chapter_stats import load_batch_settings, load_length_settings
from common import atomic_write_json, load_json, safe_workspace_path, validate_workspace_layout
from production_state import load_production_settings


def _positive(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Persist project writing and future batch defaults.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--min-chars", type=int)
    parser.add_argument("--target-chars", type=int)
    parser.add_argument("--soft-max-chars", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--planning-window", type=int)
    parser.add_argument("--writer-pool-size", type=int)
    parser.add_argument("--blind-reader-count", type=int)
    parser.add_argument("--lookahead-blocks", type=int, help="Default speculative five-chapter lead; 1-10, advisory wavefront cap")
    parser.add_argument(
        "--batch-start",
        type=int,
        help="Explicitly anchor the active batch at the next uncommitted chapter; use for imported/existing projects",
    )
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        lengths = load_length_settings(root)
        future_batch = load_batch_settings(root, allow_legacy_batch_size=True)
        production = load_production_settings(root)
        if args.min_chars is not None:
            lengths["minimum_effective_chars"] = _positive(args.min_chars, "--min-chars")
        if args.target_chars is not None:
            lengths["target_effective_chars"] = _positive(args.target_chars, "--target-chars")
        if args.soft_max_chars is not None:
            lengths["soft_maximum_effective_chars"] = _positive(args.soft_max_chars, "--soft-max-chars")
        if lengths["target_effective_chars"] < lengths["minimum_effective_chars"]:
            raise ValueError("target chars must be at least minimum chars")
        if lengths["soft_maximum_effective_chars"] < lengths["target_effective_chars"]:
            raise ValueError("soft maximum chars must be at least target chars")

        if args.batch_size is not None and args.batch_size != 5:
            raise ValueError("--batch-size is permanently fixed at 5; final 1-4 chapter tails are handled separately by 主Agent")
        future_batch["batch_size"] = 5
        if args.planning_window is not None:
            future_batch["planning_window"] = _positive(args.planning_window, "--planning-window")
        if future_batch["planning_window"] < 5:
            raise ValueError("planning window must be at least 5")
        if args.writer_pool_size is not None:
            production["writer_pool_size"] = _positive(args.writer_pool_size, "--writer-pool-size")
        if args.blind_reader_count is not None:
            production["blind_reader_count"] = _positive(args.blind_reader_count, "--blind-reader-count")
        if args.lookahead_blocks is not None:
            production["speculative_lookahead_blocks"] = _positive(args.lookahead_blocks, "--lookahead-blocks")
        if not 1 <= production["writer_pool_size"] <= 10:
            raise ValueError("writer pool size must be between 1 and 10")
        if not 1 <= production["blind_reader_count"] <= 3:
            raise ValueError("blind reader count per five-chapter block must be between 1 and 3")
        if not 1 <= production["speculative_lookahead_blocks"] <= 10:
            raise ValueError("speculative lookahead blocks must be between 1 and 10")

        current_path = safe_workspace_path(root, "state/current.json", allow_missing=False)
        current = load_json(current_path, required=True)
        if not isinstance(current, dict):
            raise ValueError("state/current.json must be an object")
        latest = current.get("latest_chapter", 0)
        if not isinstance(latest, int) or isinstance(latest, bool) or latest < 0:
            raise ValueError("state/current.json latest_chapter must be a non-negative integer")

        if "batch" in current:
            active = load_active_batch(root, current)
        else:
            # An older project has no active batch state. Use the new configured size
            # when anchoring at its next uncommitted chapter.
            active = make_batch(batch_id=1, start_chapter=latest + 1, batch_size=5)

        review_path = safe_workspace_path(root, batch_review_relative(active), allow_missing=True)
        batch_has_started = latest >= active["start_chapter"] or review_path.exists() or isinstance(current.get("final_tail"), dict)
        if args.batch_start is not None:
            if args.batch_start != latest + 1:
                raise ValueError(f"--batch-start must equal the next uncommitted chapter {latest + 1}")
            if batch_has_started and args.batch_start != active["start_chapter"]:
                raise ValueError(
                    "cannot re-anchor a started or frozen batch; finish or recover the active batch first"
                )
            active = make_batch(
                batch_id=active["batch_id"],
                start_chapter=args.batch_start,
                batch_size=5,
            )
        elif args.batch_size is not None and not batch_has_started:
            active = make_batch(
                batch_id=active["batch_id"],
                start_chapter=active["start_chapter"],
                batch_size=5,
            )

        next_current = dict(current)
        next_current["batch"] = active
        settings_payload = {
            "schema": 1,
            "chapter_length": lengths,
            "batch": future_batch,
            "production": production,
        }
        # Persist only after every value and re-anchor decision has been validated.
        settings_path = safe_workspace_path(root, "state/writing-settings.json", allow_missing=True)
        atomic_write_json(settings_path, settings_payload)
        atomic_write_json(current_path, next_current)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "configured": True,
        "chapter_length": lengths,
        "future_batch_defaults": future_batch,
        "active_batch": active,
        "production": production,
        "note": "Review batches are permanently fixed at 5. A final 1-4 chapter tail is an explicit main-agent-only exception, not a configurable batch size.",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
