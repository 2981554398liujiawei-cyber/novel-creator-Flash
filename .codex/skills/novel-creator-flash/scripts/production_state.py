#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import load_json, safe_workspace_path

WRITER_BLOCK_SIZE = 5
WRITER_AGENTS = tuple(f"novel-fast-writer-{index}" for index in range(1, 11))
READER_AGENTS = (
    "novel-fast-reader-flow",
    "novel-fast-reader-hook",
    "novel-fast-reader-character",
)
DEFAULT_PRODUCTION_SETTINGS = {
    "writer_pool_size": 5,
    "blind_reader_count": 1,
    "speculative_lookahead_blocks": 2,
}


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def reader_agents_for(blind_reader_count: int) -> tuple[str, ...]:
    """One holistic reader is enough for speed; extra seats add Hook then Character."""
    count = _integer(blind_reader_count, "production.blind_reader_count", minimum=1, maximum=len(READER_AGENTS))
    if count == 1:
        return ("novel-fast-reader-flow",)
    if count == 2:
        return ("novel-fast-reader-flow", "novel-fast-reader-hook")
    return READER_AGENTS


def load_production_settings(root: Path) -> dict[str, int]:
    path = safe_workspace_path(root, "state/writing-settings.json", allow_missing=True)
    data = load_json(path, default=None)
    if data is None:
        return dict(DEFAULT_PRODUCTION_SETTINGS)
    if not isinstance(data, dict):
        raise ValueError("state/writing-settings.json must be an object")
    raw = data.get("production")
    if raw is None:
        return dict(DEFAULT_PRODUCTION_SETTINGS)
    if not isinstance(raw, dict):
        raise ValueError("state/writing-settings.json production must be an object")
    return {
        "writer_pool_size": _integer(raw.get("writer_pool_size"), "production.writer_pool_size", minimum=1, maximum=len(WRITER_AGENTS)),
        "blind_reader_count": _integer(raw.get("blind_reader_count"), "production.blind_reader_count", minimum=1, maximum=len(READER_AGENTS)),
        "speculative_lookahead_blocks": _integer(raw.get("speculative_lookahead_blocks", DEFAULT_PRODUCTION_SETTINGS["speculative_lookahead_blocks"]), "production.speculative_lookahead_blocks", minimum=1, maximum=len(WRITER_AGENTS)),
    }


def production_run_relative(start_chapter: int, end_chapter: int) -> str:
    return f".novel/production/run-{start_chapter:04d}-{end_chapter:04d}"


def production_manifest_relative_from_current(current: dict[str, Any]) -> str:
    run = current.get("production_run")
    if not isinstance(run, dict) or not isinstance(run.get("manifest"), str):
        raise ValueError("no active production run; run prepare-production first")
    return run["manifest"]


def raw_output_relative(run_root: str, chapter: int, agent: str) -> str:
    return f"{run_root}/raw/{agent}/chapter-{chapter:04d}.md"


def report_output_relative(run_root: str, start_chapter: int, end_chapter: int, agent: str) -> str:
    return f"{run_root}/reports/{agent}-block-{start_chapter:04d}-{end_chapter:04d}.json"


def build_assignments(start_chapter: int, writer_count: int, *, run_root: str | None = None) -> list[dict[str, Any]]:
    agents = WRITER_AGENTS[:writer_count]
    if run_root is None:
        run_end = start_chapter + writer_count * WRITER_BLOCK_SIZE - 1
        run_root = production_run_relative(start_chapter, run_end)
    result: list[dict[str, Any]] = []
    for index, agent in enumerate(agents):
        block_start = start_chapter + index * WRITER_BLOCK_SIZE
        block_end = block_start + WRITER_BLOCK_SIZE - 1
        result.append({
            "block_id": index + 1,
            "writer": agent,
            "start_chapter": block_start,
            "end_chapter": block_end,
            "context": f"state/context/chapter-{block_start:04d}-writer-context.md",
            "outputs": [raw_output_relative(run_root, chapter, agent) for chapter in range(block_start, block_end + 1)],
            "report": report_output_relative(run_root, block_start, block_end, agent),
        })
    return result


def block_for_range(manifest: dict[str, Any], start_chapter: int, end_chapter: int) -> dict[str, Any] | None:
    for item in manifest.get("assignments", []):
        if isinstance(item, dict) and item.get("start_chapter") == start_chapter and item.get("end_chapter") == end_chapter:
            return item
    return None

def validate_manifest(manifest: Any) -> list[str]:
    if not isinstance(manifest, dict) or manifest.get("schema") != 2:
        return ["production manifest must be a schema 2 object"]
    errors: list[str] = []
    for key in ("start_chapter", "end_chapter", "planned_chapters", "parallel_chapters", "writer_block_size", "writer_count"):
        value=manifest.get(key)
        if not isinstance(value,int) or isinstance(value,bool): errors.append(f"manifest {key} must be an integer")
    if errors: return errors
    start=manifest["start_chapter"]; end=manifest["end_chapter"]; planned=manifest["planned_chapters"]
    parallel=manifest["parallel_chapters"]; writer_count=manifest["writer_count"]
    if start<1 or end<start: errors.append("manifest chapter range is invalid")
    if planned != end-start+1: errors.append("manifest planned_chapters must match range")
    if manifest["writer_block_size"] != WRITER_BLOCK_SIZE: errors.append(f"manifest writer_block_size must be {WRITER_BLOCK_SIZE}")
    if not 0 <= writer_count <= len(WRITER_AGENTS): errors.append("manifest writer_count is out of range")
    if parallel != writer_count*WRITER_BLOCK_SIZE: errors.append("manifest parallel_chapters must equal writer_count * 5")
    if parallel>planned: errors.append("manifest parallel_chapters cannot exceed planned_chapters")
    assignments=manifest.get("assignments")
    if not isinstance(assignments,list) or len(assignments)!=writer_count:
        errors.append("manifest assignments must contain exactly writer_count blocks")
        return errors
    run_root=production_run_relative(start,end); expected=build_assignments(start,writer_count,run_root=run_root)
    seen_outputs=set(); seen_reports=set(); seen_writers=set()
    for index,(actual,want) in enumerate(zip(assignments,expected),start=1):
        if not isinstance(actual,dict): errors.append(f"assignment {index} must be an object"); continue
        for key in ("block_id","writer","start_chapter","end_chapter","outputs","report"):
            if actual.get(key)!=want.get(key): errors.append(f"assignment {index} {key} does not match canonical five-chapter assignment")
        # V7-era schema-2 manifests may not contain `context`; derive it instead of rejecting an active wave during upgrade.
        if actual.get("context") not in (None,want.get("context")):
            errors.append(f"assignment {index} context is invalid")
        writer=actual.get("writer")
        if writer in seen_writers: errors.append(f"assignment {index} duplicates writer {writer}")
        seen_writers.add(writer)
        outputs=actual.get("outputs")
        if not isinstance(outputs,list) or len(outputs)!=WRITER_BLOCK_SIZE:
            errors.append(f"assignment {index} must contain exactly five outputs")
        else:
            for output in outputs:
                if output in seen_outputs: errors.append(f"duplicate production output: {output}")
                seen_outputs.add(output)
        report=actual.get("report")
        if report in seen_reports: errors.append(f"duplicate production report: {report}")
        seen_reports.add(report)
    remainder_count=planned-parallel
    remainder=manifest.get("main_agent_remainder", manifest.get("main_agent_tail"))
    if remainder_count:
        expected_start=start+parallel
        if not isinstance(remainder,dict): errors.append("manifest main_agent_remainder is required for a 1-4 chapter remainder")
        else:
            if remainder.get("writer")!="main-agent": errors.append("main_agent_remainder.writer must be main-agent")
            if remainder.get("start_chapter")!=expected_start or remainder.get("end_chapter")!=end or remainder.get("chapter_count")!=remainder_count:
                errors.append("main_agent_remainder range/count mismatch")
    elif remainder not in (None,{}): errors.append("main_agent_remainder must be empty when no remainder exists")
    return errors

