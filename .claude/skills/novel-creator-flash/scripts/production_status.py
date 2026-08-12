#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from chapter_stats import analyze_chapter_text, load_length_settings
from common import load_json, read_text, safe_workspace_path, sha256_text, title_chapter_number, validate_workspace_layout
from production_state import production_manifest_relative_from_current

REPORT_LIST_FIELDS = (
    "newly_invented_details", "character_micro_changes", "new_promises",
    "motifs_or_objects", "strong_lines_or_moments", "possible_continuity_risks",
)


def validate_report(data: object, assignment: dict) -> list[str]:
    errors=[]
    if not isinstance(data, dict) or data.get("schema") != 1:
        return ["report must be a schema 1 object"]
    for key in ("writer", "start_chapter", "end_chapter"):
        if data.get(key) != assignment.get(key if key != "writer" else "writer"):
            errors.append(f"report {key} does not match assignment")
    for key in REPORT_LIST_FIELDS:
        value=data.get(key)
        if not isinstance(value,list) or any(not isinstance(x,str) for x in value):
            errors.append(f"report {key} must be a list of strings")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Check every five-chapter writer block, its five raw files, and persistent creative-risk report.")
    parser.add_argument("workspace", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        current = load_json(root / "state/current.json", required=True)
        if not isinstance(current, dict):
            raise ValueError("state/current.json must be an object")
        manifest_rel = production_manifest_relative_from_current(current)
        manifest_path = safe_workspace_path(root, manifest_rel, allow_missing=False)
        manifest = load_json(manifest_path, required=True)
        if not isinstance(manifest, dict) or manifest.get("schema") != 2:
            raise ValueError("production manifest must be a schema 2 object")
        settings = load_length_settings(root)
        blocks=[]; all_ready=True; all_risks=[]
        for assignment in manifest.get("assignments", []):
            if not isinstance(assignment, dict): raise ValueError("production assignment must be an object")
            chapter_results=[]; block_ready=True
            for chapter, output in zip(range(assignment["start_chapter"], assignment["end_chapter"]+1), assignment.get("outputs", [])):
                path=safe_workspace_path(root, output, allow_missing=True)
                item={"chapter":chapter,"output":output,"exists":path.is_file()}
                if not path.is_file():
                    item["status"]="missing"; block_ready=False
                else:
                    if getattr(path.stat(),"st_nlink",1)!=1: raise ValueError(f"hard-linked writer output is not allowed: {output}")
                    text=read_text(path, required=True); heading=title_chapter_number(text)
                    item["heading_chapter"]=heading
                    if heading!=chapter: item["status"]="wrong_chapter_heading"; block_ready=False
                    stats=analyze_chapter_text(text,settings); item.update(stats); item["sha256"]=sha256_text(text.rstrip()+"\n")
                    if not stats["passes_minimum"]: block_ready=False
                chapter_results.append(item)
            report_rel=assignment.get("report")
            report_path=safe_workspace_path(root,report_rel,allow_missing=True) if isinstance(report_rel,str) else None
            report_errors=[]; report_data=None
            if report_path is None or not report_path.is_file(): report_errors=["report missing"]
            else:
                if getattr(report_path.stat(),"st_nlink",1)!=1: raise ValueError(f"hard-linked writer report is not allowed: {report_rel}")
                report_data=load_json(report_path,required=True); report_errors=validate_report(report_data,assignment)
            if report_errors: block_ready=False
            risks=[] if not isinstance(report_data,dict) else report_data.get("possible_continuity_risks",[])
            if isinstance(risks,list):
                for risk in risks:
                    if isinstance(risk,str) and risk.strip(): all_risks.append(f"{assignment['start_chapter']}-{assignment['end_chapter']}: {risk.strip()}")
            blocks.append({"writer":assignment.get("writer"),"range":f"{assignment['start_chapter']}-{assignment['end_chapter']}","ready":block_ready,"chapters":chapter_results,"report":report_rel,"report_errors":report_errors,"continuity_risks":risks})
            all_ready = all_ready and block_ready
    except (ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "production_run": f"{manifest['start_chapter']}-{manifest['end_chapter']}",
        "all_ready": all_ready,
        "blocks": blocks,
        "continuity_risk_detected": bool(all_risks),
        "continuity_risks": all_risks,
        "main_agent_tail": manifest.get("main_agent_tail"),
        "next": "主Agent按五章块顺序整合 raw；每个写手块内部已经顺序写作。若 manifest 有 main_agent_tail，则最后 1-4 章由主Agent直接写，不启动 Writer，并在收尾时用 prepare-review --final-tail-count N。任何持久化 continuity risk 都必须在对应五章 prepare-review 时按 high 处理。",
    },ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main())
