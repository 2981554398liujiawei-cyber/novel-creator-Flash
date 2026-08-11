#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common import (
    atomic_write_text,
    ensure_no_symlink_chain,
    load_events,
    load_json,
    output_under,
    read_text,
    relevance_score,
    require_int,
    token_set,
    validate_workspace_layout,
)


@dataclass(frozen=True)
class Result:
    score: float
    kind: str
    source: str
    title: str
    excerpt: str


def clipped(text: str, limit: int = 900) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def best_excerpt(text: str, query: str, limit: int = 900) -> str:
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    if not blocks:
        return clipped(text, limit)
    ranked = sorted(blocks, key=lambda block: relevance_score(query, block), reverse=True)
    selected: list[str] = []
    used = 0
    for block in ranked:
        score = relevance_score(query, block)
        if score <= 0 and selected:
            break
        clean = " ".join(block.split())
        if len(clean) > limit:
            clean = clipped(clean, limit)
        if used + len(clean) + 3 > limit:
            continue
        selected.append(clean)
        used += len(clean) + 3
        if len(selected) >= 3:
            break
    return " … ".join(selected) if selected else clipped(text, limit)


def json_excerpt(data: Any, query: str, limit: int = 900) -> str:
    if not isinstance(data, dict):
        return clipped(json.dumps(data, ensure_ascii=False, sort_keys=True), limit)
    scored: list[tuple[float, str]] = []
    for key, value in data.items():
        rendered = json.dumps({key: value}, ensure_ascii=False, sort_keys=True)
        scored.append((relevance_score(query, rendered), rendered))
    scored.sort(key=lambda item: item[0], reverse=True)
    chosen: list[str] = []
    used = 0
    for score, rendered in scored:
        if score <= 0 and chosen:
            break
        if used + len(rendered) + 2 > limit:
            continue
        chosen.append(rendered)
        used += len(rendered) + 2
    if not chosen:
        return clipped(json.dumps(data, ensure_ascii=False, sort_keys=True), limit)
    return " … ".join(chosen)


def collect(root: Path, query: str, *, strict_events: bool = False) -> list[Result]:
    root = root.resolve(strict=True)
    validate_workspace_layout(root)
    results: list[Result] = []

    entity_root = root / "state" / "entities"
    for path in entity_root.glob("*/*.json"):
        ensure_no_symlink_chain(path, root, allow_missing=False)
        data = load_json(path, default={}) or {}
        rendered = json.dumps(data, ensure_ascii=False, sort_keys=True)
        score = relevance_score(query, rendered)
        if score > 0:
            results.append(Result(score, "entity", path.relative_to(root).as_posix(), str(data.get("name") or data.get("id") or path.stem), json_excerpt(data, query)))

    arc_root = root / "state" / "arc-summaries"
    for path in arc_root.glob("*.md"):
        ensure_no_symlink_chain(path, root, allow_missing=False)
        text = read_text(path)
        score = relevance_score(query, text)
        if score > 0:
            title = next((line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("#")), path.stem)
            results.append(Result(score, "arc", path.relative_to(root).as_posix(), title, best_excerpt(text, query)))

    for relative in ["canon/facts.md", "canon/changes.md", "canon/source-index.md"]:
        path = root / relative
        ensure_no_symlink_chain(path, root, allow_missing=True)
        text = read_text(path)
        score = relevance_score(query, text)
        if score > 0:
            results.append(Result(score, "canon", relative, path.stem, best_excerpt(text, query)))

    chapter_rows: list[tuple[Path, dict[str, Any]]] = []
    for path in (root / "state" / "chapters").glob("chapter-*.json"):
        ensure_no_symlink_chain(path, root, allow_missing=False)
        data = load_json(path, default={}) or {}
        if isinstance(data, dict):
            chapter_rows.append((path, data))
    valid_chapter_rows: list[tuple[Path, dict[str, Any], int]] = []
    for path, data in chapter_rows:
        try:
            number = require_int(data.get("chapter"), f"{path.relative_to(root).as_posix()}.chapter", minimum=1)
        except ValueError:
            if strict_events:
                raise
            continue
        valid_chapter_rows.append((path, data, number))
    latest = max((number for _, _, number in valid_chapter_rows), default=0)
    for path, data, number in valid_chapter_rows:
        # Chapter numbers, schema values and broad outline-node IDs are poor memory signals:
        # thousands of chapters often share them and they crowd out distinctive old facts.
        searchable = {
            key: data.get(key)
            for key in ("title", "summary", "entities", "events", "depends_on_events", "knowledge_used", "state_used")
            if data.get(key) not in (None, "", [], {})
        }
        rendered = json.dumps(searchable, ensure_ascii=False, sort_keys=True)
        score = relevance_score(query, rendered)
        if score > 0:
            # A small recency tie-breaker helps generic equal-score summaries without burying old exact matches.
            if latest:
                score += 0.5 * number / latest
            results.append(Result(score, "chapter", path.relative_to(root).as_posix(), f"第{data.get('chapter', '?')}章 {data.get('title', '')}".strip(), json_excerpt(data, query)))

    events_path = root / "state" / "events" / "events.jsonl"
    try:
        events = load_events(events_path)
    except ValueError:
        if strict_events:
            raise
        events = []
    for line_number, data in enumerate(events, 1):
        rendered = json.dumps(data, ensure_ascii=False, sort_keys=True)
        score = relevance_score(query, rendered)
        if score > 0:
            chapter = data.get("chapter") if isinstance(data.get("chapter"), int) and not isinstance(data.get("chapter"), bool) else 0
            if latest and chapter:
                score += 0.4 * chapter / latest
            results.append(Result(score, "event", f"state/events/events.jsonl:{line_number}", str(data.get("id") or data.get("type") or "event"), json_excerpt(data, query)))

    results.sort(key=lambda item: (-item.score, item.kind, item.source))
    return results


def _near_duplicate(a: Result, b: Result) -> bool:
    a_clean = re.sub(r"\s+", "", a.excerpt.casefold())
    b_clean = re.sub(r"\s+", "", b.excerpt.casefold())
    if a_clean and a_clean == b_clean:
        return True
    ta, tb = token_set(a.excerpt), token_set(b.excerpt)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.88


def balanced_select(results: list[Result], limit: int, excluded_sources: set[str] | None = None) -> list[Result]:
    excluded = excluded_sources or set()
    available = [item for item in results if item.source not in excluded]
    if limit <= 0:
        return []
    selected: list[Result] = []
    selected_sources: set[str] = set()
    kinds = ("canon", "arc", "event", "chapter", "entity")
    # Reserve one seat per represented type.
    for kind in kinds:
        candidate = next((item for item in available if item.kind == kind and item.source not in selected_sources), None)
        if candidate is not None:
            selected.append(candidate)
            selected_sources.add(candidate.source)
            if len(selected) >= limit:
                return selected
    # Fill the remaining seats while suppressing near-identical generic memories and limiting any one type.
    kind_counts = {kind: sum(item.kind == kind for item in selected) for kind in kinds}
    soft_cap = max(2, (limit + len(kinds) - 1) // len(kinds) + 1)
    for item in available:
        if item.source in selected_sources:
            continue
        if kind_counts.get(item.kind, 0) >= soft_cap:
            continue
        if any(_near_duplicate(item, chosen) for chosen in selected):
            continue
        selected.append(item)
        selected_sources.add(item.source)
        kind_counts[item.kind] = kind_counts.get(item.kind, 0) + 1
        if len(selected) >= limit:
            break
    # Do not fill a quota with near-identical generic memories. Returning fewer, distinct
    # results is more useful than padding the packet with dozens of interchangeable chapters.
    if len(selected) < limit:
        for item in available:
            if item.source in selected_sources or any(_near_duplicate(item, chosen) for chosen in selected):
                continue
            selected.append(item)
            selected_sources.add(item.source)
            if len(selected) >= limit:
                break
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Search structured novel memory without reading the full book.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--output", default="memory-search.md")
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    try:
        results = balanced_select(collect(root, args.query, strict_events=True), max(1, args.limit))
    except ValueError as exc:
        parser.error(str(exc))
    lines = ["# 相关记忆检索", "", f"查询：{args.query}", ""]
    if not results:
        lines.append("未找到相关结构化记忆。")
    for result in results:
        lines.extend([
            f"## {result.title}", "",
            f"- 类型：{result.kind}",
            f"- 来源：`{result.source}`",
            f"- 相关度：{result.score:.2f}", "",
            result.excerpt, "",
        ])
    output = output_under(root, "audits", args.output, "memory-search.md")
    atomic_write_text(output, "\n".join(lines).rstrip() + "\n")
    print(json.dumps({"results": len(results), "output": output.relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
