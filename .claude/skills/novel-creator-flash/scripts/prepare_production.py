#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch_state import load_active_batch
from common import atomic_write_json, load_json, safe_workspace_path, utc_timestamp, validate_workspace_layout
from production_state import WRITER_BLOCK_SIZE, build_assignments, load_production_settings, production_run_relative, reader_agents_for


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan one rapid-production run: each Writer receives one contiguous five-chapter block; a final 1-4 chapter remainder is reserved for 主Agent.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--writers", type=int, help="Maximum active writer seats for this run; default uses configured pool size")
    parser.add_argument("--chapters", type=int, help="Requested chapters for this run. Full five-chapter blocks go to Writers; a 1-4 chapter tail goes to 主Agent")
    parser.add_argument("--force", action="store_true", help="Replace an unstarted production run")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        current_path = safe_workspace_path(root, "state/current.json", allow_missing=False)
        current = load_json(current_path, required=True)
        if not isinstance(current, dict):
            raise ValueError("state/current.json must be an object")
        latest = current.get("latest_chapter", 0)
        if not isinstance(latest, int) or isinstance(latest, bool) or latest < 0:
            raise ValueError("latest_chapter must be a non-negative integer")
        batch = load_active_batch(root, current)
        if batch["start_chapter"] != latest + 1:
            raise ValueError("active review batch must start at the next uncommitted chapter before production is prepared")
        if isinstance(current.get("final_tail"), dict):
            raise ValueError("an active final-tail review unit already exists; finish it before preparing a new production run")

        settings = load_production_settings(root)
        seat_cap = args.writers if args.writers is not None else settings["writer_pool_size"]
        if not isinstance(seat_cap, int) or isinstance(seat_cap, bool) or not 1 <= seat_cap <= settings["writer_pool_size"]:
            raise ValueError(f"--writers must be between 1 and configured writer_pool_size {settings['writer_pool_size']}")
        chapters = args.chapters if args.chapters is not None else seat_cap * WRITER_BLOCK_SIZE
        if not isinstance(chapters, int) or isinstance(chapters, bool) or chapters < 1:
            raise ValueError("--chapters must be a positive integer")

        parallel_chapters = (chapters // WRITER_BLOCK_SIZE) * WRITER_BLOCK_SIZE
        tail_count = chapters - parallel_chapters
        needed_writers = parallel_chapters // WRITER_BLOCK_SIZE
        if needed_writers > seat_cap:
            raise ValueError(f"{parallel_chapters} parallel chapters require {needed_writers} writers, but this run allows only {seat_cap}")
        writers = needed_writers
        start = latest + 1
        end = start + chapters - 1
        parallel_end = start + parallel_chapters - 1 if parallel_chapters else None
        tail_start = start + parallel_chapters if tail_count else None
        run_root = production_run_relative(start, end)
        manifest_rel = run_root + "/manifest.json"
        manifest_path = safe_workspace_path(root, manifest_rel, allow_missing=True)

        old_run = current.get("production_run")
        if isinstance(old_run, dict):
            old_end = old_run.get("end_chapter")
            if isinstance(old_end, int) and latest < old_end and not args.force:
                raise ValueError("an unfinished production run already exists; finish it or use --force before writer outputs exist")
            if args.force and isinstance(old_run.get("manifest"), str):
                old_manifest = safe_workspace_path(root, old_run["manifest"], allow_missing=True)
                old_data = load_json(old_manifest, default=None)
                if isinstance(old_data, dict):
                    for a in old_data.get("assignments", []):
                        if isinstance(a, dict):
                            paths = list(a.get("outputs", [])) + [a.get("report")]
                            if any(isinstance(x, str) and safe_workspace_path(root, x, allow_missing=True).exists() for x in paths):
                                raise ValueError("cannot force-replace a production run after writer outputs or reports exist")

        assignments = build_assignments(start, writers, run_root=run_root) if writers else []
        for sub in ("raw", "reports"):
            safe_workspace_path(root, run_root + "/" + sub, allow_missing=True).mkdir(parents=True, exist_ok=True)
        readers = list(reader_agents_for(settings["blind_reader_count"]))
        reader_jobs = []
        for assignment in assignments:
            for reader in readers:
                reader_jobs.append({
                    "range": f"{assignment['start_chapter']}-{assignment['end_chapter']}",
                    "start_chapter": assignment["start_chapter"],
                    "end_chapter": assignment["end_chapter"],
                    "reader": reader,
                })
        main_agent_tail = None
        if tail_count:
            main_agent_tail = {
                "writer": "main-agent",
                "start_chapter": tail_start,
                "end_chapter": end,
                "chapter_count": tail_count,
                "instruction": "不足五章，不派 Writer；由主Agent顺序创作并以 --final-tail-count 进入终局审读。",
            }
            for reader in readers:
                reader_jobs.append({
                    "range": f"{tail_start}-{end}",
                    "start_chapter": tail_start,
                    "end_chapter": end,
                    "reader": reader,
                    "review_kind": "final_tail",
                })

        record = {
            "schema": 2,
            "start_chapter": start,
            "end_chapter": end,
            "planned_chapters": chapters,
            "parallel_chapters": parallel_chapters,
            "writer_block_size": WRITER_BLOCK_SIZE,
            "writer_count": writers,
            "reader_count_per_block": settings["blind_reader_count"],
            "created_at": utc_timestamp(),
            "assignments": assignments,
            "main_agent_tail": main_agent_tail,
            "reader_jobs": reader_jobs,
        }
        atomic_write_json(manifest_path, record)
        next_current = dict(current)
        next_current["production_run"] = {
            "start_chapter": start,
            "end_chapter": end,
            "parallel_end_chapter": parallel_end,
            "writer_count": writers,
            "writer_block_size": WRITER_BLOCK_SIZE,
            "main_agent_tail": main_agent_tail,
            "manifest": manifest_rel,
        }
        atomic_write_json(current_path, next_current)
    except ValueError as exc:
        parser.error(str(exc))

    if writers:
        planning_rule = f"主Agent先规划全部 {chapters} 章；其中 {parallel_chapters} 章分给 {writers} 个 Writer，每席连续 {WRITER_BLOCK_SIZE} 章。"
    else:
        planning_rule = f"任务只有 {chapters} 章（不足 5）；不启动 Writer，由主Agent直接顺序创作。"
    if tail_count and writers:
        planning_rule += f" 最后 {tail_count} 章由主Agent直接顺序创作。"

    print(json.dumps({
        "prepared": True,
        "production_run": f"{start}-{end}",
        "planned_chapters": chapters,
        "parallel_chapters": parallel_chapters,
        "writer_count": writers,
        "main_agent_tail": main_agent_tail,
        "planning_rule": planning_rule,
        "manifest": manifest_rel,
        "assignments": assignments,
        "reader_jobs": reader_jobs,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
