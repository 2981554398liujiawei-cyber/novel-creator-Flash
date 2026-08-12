#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch_review import chapter_key, current_prose_hash
from batch_state import load_active_batch, load_active_review_unit, make_final_tail, review_record_relative
from blind_packet import build_blind_packet
from common import atomic_write_json, load_json, safe_workspace_path, utc_timestamp, validate_workspace_layout
from production_state import load_production_settings, production_manifest_relative_from_current, reader_agents_for


def persisted_writer_risks(root: Path, current: dict, unit: dict) -> list[str]:
    if unit.get("review_kind") == "final_tail":
        return []
    try:
        manifest_rel = production_manifest_relative_from_current(current)
    except ValueError:
        return []
    manifest_path = safe_workspace_path(root, manifest_rel, allow_missing=True)
    if not manifest_path.is_file():
        return []
    manifest = load_json(manifest_path, required=True)
    if not isinstance(manifest, dict) or manifest.get("schema") != 2:
        return []
    risks_out: list[str] = []
    for block in manifest.get("assignments", []):
        if not isinstance(block, dict):
            continue
        block_start = block.get("start_chapter")
        block_end = block.get("end_chapter")
        report_rel = block.get("report")
        if not isinstance(block_start, int) or not isinstance(block_end, int) or not isinstance(report_rel, str):
            continue
        if block_end < unit["start_chapter"] or block_start > unit["end_chapter"]:
            continue
        report_path = safe_workspace_path(root, report_rel, allow_missing=True)
        if not report_path.is_file():
            continue
        report = load_json(report_path, required=True)
        risks = report.get("possible_continuity_risks", []) if isinstance(report, dict) else []
        for item in risks:
            if isinstance(item, str) and item.strip():
                risks_out.append(f"{block_start}-{block_end}: {item.strip()}")
    return list(dict.fromkeys(risks_out))


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze a fixed five-chapter Flash review unit, or an explicit 1-4 chapter main-agent final tail.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--force", action="store_true", help="Replace an existing unfinalized review record")
    parser.add_argument("--continuity-risk", choices=("low", "high"), required=True, help="Must be explicitly assessed; high requires the lightweight reviewer")
    parser.add_argument("--risk-reason", action="append", default=[], help="Short extra reason for high continuity risk; may be repeated")
    parser.add_argument("--reader-brief", default="", help="Short target-reader description written into the blind packet")
    parser.add_argument(
        "--final-tail-count",
        type=int,
        choices=(1, 2, 3, 4),
        help="Explicit terminal exception: 主Agent wrote the final 1-4 chapters; Writer agents are never used for this tail",
    )
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
            raise ValueError("state/current.json latest_chapter must be a non-negative integer")
        active_batch = load_active_batch(root, current)
        if args.final_tail_count is not None:
            if latest + 1 != active_batch["start_chapter"]:
                raise ValueError("final tail must start at the next uncommitted chapter and cannot be declared after partial batch commits")
            declared = make_final_tail(active_batch, args.final_tail_count)
            existing_tail = current.get("final_tail")
            if existing_tail is not None and existing_tail != declared and not args.force:
                raise ValueError("a different final tail is already declared; use --force only after reviewing the existing tail state")
            current = dict(current)
            current["final_tail"] = declared
            atomic_write_json(current_path, current)
        unit = load_active_review_unit(root, current)
        writer_risks = persisted_writer_risks(root, current, unit)
        if writer_risks and args.continuity_risk != "high":
            raise ValueError("Writer report contains persisted possible_continuity_risks; --continuity-risk must be high")
        manual_reasons = [item.strip() for item in args.risk_reason if item.strip()]
        writer_reasons = [f"writer-report: {item}" for item in writer_risks]
        # Preserve persistent Writer evidence before manual reasons when truncating.
        risk_reasons = list(dict.fromkeys(writer_reasons + manual_reasons))[:8]
        if args.continuity_risk == "high" and not risk_reasons:
            raise ValueError("--continuity-risk high requires at least one --risk-reason or persisted Writer risk")
        if any(len(item) > 160 for item in risk_reasons):
            raise ValueError("each continuity risk reason must be at most 160 characters")

        path = safe_workspace_path(root, review_record_relative(unit), allow_missing=True)
        existing = load_json(path, default=None)
        if existing is not None and not args.force:
            raise ValueError(f"review record already exists: {path.relative_to(root).as_posix()}")
        if isinstance(existing, dict) and existing.get("finalized") is True:
            raise ValueError("finalized review record cannot be replaced")
        production = load_production_settings(root)
        hashes = {chapter_key(chapter): current_prose_hash(root, chapter) for chapter in range(unit["start_chapter"], unit["end_chapter"] + 1)}
        packet_path, packet_hash = build_blind_packet(root, unit, reader_brief=args.reader_brief)
        record = {
            "schema": 1,
            "review_kind": unit.get("review_kind", "batch"),
            "batch_id": unit["batch_id"],
            "start_chapter": unit["start_chapter"],
            "end_chapter": unit["end_chapter"],
            "batch_size": unit["batch_size"],
            "prepared_at": utc_timestamp(),
            "frozen_hashes": hashes,
            "blind_packet": {"path": packet_path, "sha256": packet_hash},
            "first_reader": {
                "status": "pending",
                "required_count": production["blind_reader_count"],
                "completed_readers": [],
                "available_readers": list(reader_agents_for(production["blind_reader_count"])),
                "verdict": "", "ending_pull": "", "revision_applied": None,
                "issue_tags": [], "highest_value_revision": "",
            },
            "continuity": {
                "risk_level": args.continuity_risk,
                "risk_reasons": risk_reasons,
                "writer_report_risk_count": len(writer_risks),
                "status": "pending", "checked_by": "", "blocking_count": None, "warning_count": None,
            },
            "finalized": False, "finalized_at": None, "final_hashes": dict(hashes),
        }
        atomic_write_json(path, record)
    except (ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "prepared": True,
        "review_kind": unit.get("review_kind", "batch"),
        "range": f"{unit['start_chapter']}-{unit['end_chapter']}",
        "output": path.relative_to(root).as_posix(),
        "blind_packet": packet_path,
        "writer_report_risks": writer_risks,
        "next": "Run the configured blind-reader panel using only the exact blind-packet path. low uses 主Agent continuity; high requires novel-fast-continuity-reviewer. Revise, then finalize-review.",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
