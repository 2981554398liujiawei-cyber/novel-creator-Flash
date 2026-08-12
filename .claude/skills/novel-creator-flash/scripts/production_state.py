#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import load_json, safe_workspace_path

WRITER_BLOCK_SIZE = 5
WRITER_AGENTS = tuple(f"novel-fast-writer-{index}" for index in range(1, 11))
READER_AGENTS = (
    "novel-fast-reader-flow",
    "novel-fast-reader-character",
    "novel-fast-reader-hook",
)
DEFAULT_PRODUCTION_SETTINGS = {
    "writer_pool_size": 5,
    "blind_reader_count": 1,
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
            "outputs": [raw_output_relative(run_root, chapter, agent) for chapter in range(block_start, block_end + 1)],
            "report": report_output_relative(run_root, block_start, block_end, agent),
        })
    return result


def block_for_range(manifest: dict[str, Any], start_chapter: int, end_chapter: int) -> dict[str, Any] | None:
    for item in manifest.get("assignments", []):
        if isinstance(item, dict) and item.get("start_chapter") == start_chapter and item.get("end_chapter") == end_chapter:
            return item
    return None
