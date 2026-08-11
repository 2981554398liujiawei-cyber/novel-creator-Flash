#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Any

from common import load_json, safe_workspace_path

WRITER_AGENTS = tuple(f"novel-fast-writer-{index}" for index in range(1, 6))
READER_AGENTS = (
    "novel-fast-reader-flow",
    "novel-fast-reader-character",
    "novel-fast-reader-hook",
)
DEFAULT_PRODUCTION_SETTINGS = {
    "writer_pool_size": 5,
    "blind_reader_count": 3,
}


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


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
        "writer_pool_size": _integer(raw.get("writer_pool_size"), "production.writer_pool_size", minimum=2, maximum=len(WRITER_AGENTS)),
        "blind_reader_count": _integer(raw.get("blind_reader_count"), "production.blind_reader_count", minimum=2, maximum=len(READER_AGENTS)),
    }


def production_root_relative(batch: dict[str, int]) -> str:
    return f".novel/production/batch-{batch['start_chapter']:04d}-{batch['end_chapter']:04d}"


def production_manifest_relative(batch: dict[str, int]) -> str:
    return production_root_relative(batch) + "/manifest.json"


def raw_output_relative(batch: dict[str, int], chapter: int, agent: str) -> str:
    return production_root_relative(batch) + f"/raw/chapter-{chapter:04d}-{agent}.md"


def assignments_for(batch: dict[str, int], writer_pool_size: int) -> list[dict[str, Any]]:
    agents = WRITER_AGENTS[:writer_pool_size]
    assignments: list[dict[str, Any]] = []
    for offset, chapter in enumerate(range(batch["start_chapter"], batch["end_chapter"] + 1)):
        agent = agents[offset % len(agents)]
        assignments.append({
            "chapter": chapter,
            "writer": agent,
            "wave": offset // len(agents) + 1,
            "output": raw_output_relative(batch, chapter, agent),
        })
    return assignments
