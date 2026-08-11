#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from common import atomic_write_text, load_json, normalize_entity_id, normalize_event_id, output_under, validate_workspace_layout, ensure_no_symlink_chain


def load_all_meta(root: Path) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for path in (root / "state" / "chapters").glob("chapter-*.json"):
        ensure_no_symlink_chain(path, root, allow_missing=False)
        data = load_json(path, default={}) or {}
        if not isinstance(data, dict):
            raise ValueError(f"chapter metadata must be an object: {path.name}")
        chapter = data.get("chapter")
        if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
            raise ValueError(f"chapter metadata has invalid chapter number: {path.name}")
        if chapter in result:
            raise ValueError(f"duplicate chapter metadata number: {chapter}")
        result[chapter] = data
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Find direct and transitive structured dependencies for a chapter, entity, or event.")
    parser.add_argument("workspace", nargs="?", default=".")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chapter", type=int)
    group.add_argument("--entity")
    group.add_argument("--event")
    parser.add_argument("--output", default="rewrite-impact.md")
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        metas = load_all_meta(root)
    except (ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    seed_entities: set[str] = set()
    seed_events: set[str] = set()
    seed_chapter = args.chapter or 0
    if args.chapter:
        seed = metas.get(args.chapter)
        if not seed:
            parser.error(f"chapter metadata not found: {args.chapter}")
        seed_entities.update(str(item).upper() for item in seed.get("entities", []))
        seed_events.update(str(item).upper() for item in seed.get("events", []))
    elif args.entity:
        try:
            seed_entities.add(normalize_entity_id(args.entity))
        except ValueError as exc:
            parser.error(str(exc))
    elif args.event:
        try:
            seed_events.add(normalize_event_id(args.event))
        except ValueError as exc:
            parser.error(str(exc))

    event_consumers: dict[str, set[int]] = defaultdict(set)
    chapter_events: dict[int, set[str]] = {}
    chapter_entities: dict[int, set[str]] = {}
    for chapter, meta in metas.items():
        chapter_events[chapter] = {str(item).upper() for item in meta.get("events", [])}
        chapter_entities[chapter] = {str(item).upper() for item in meta.get("entities", [])}
        for event_id in meta.get("depends_on_events", []):
            event_consumers[str(event_id).upper()].add(chapter)

    strong_reasons: dict[int, set[str]] = defaultdict(set)
    queue: deque[str] = deque(sorted(seed_events))
    seen_events = set(seed_events)
    while queue:
        event_id = queue.popleft()
        for chapter in sorted(event_consumers.get(event_id, set())):
            if seed_chapter and chapter <= seed_chapter:
                continue
            strong_reasons[chapter].add(f"依赖事件：{event_id}")
            for produced in chapter_events.get(chapter, set()):
                if produced not in seen_events:
                    seen_events.add(produced)
                    queue.append(produced)

    weak_reasons: dict[int, set[str]] = defaultdict(set)
    for chapter, entities in sorted(chapter_entities.items()):
        if seed_chapter and chapter <= seed_chapter:
            continue
        shared = seed_entities & entities
        if shared and chapter not in strong_reasons:
            weak_reasons[chapter].add("共享实体：" + ", ".join(sorted(shared)))

    lines = ["# 重写影响范围", ""]
    if args.chapter:
        lines.append(f"- 起点章节：第{args.chapter}章")
    if args.entity:
        lines.append(f"- 实体：{next(iter(seed_entities))}")
    if args.event:
        lines.append(f"- 事件：{next(iter(seed_events))}")
    lines.extend([
        f"- 起始实体：{', '.join(sorted(seed_entities)) or '无'}",
        f"- 起始事件：{', '.join(sorted(seed_events)) or '无'}",
        "",
        "## 强依赖（事件传递闭包）",
        "",
    ])
    if strong_reasons:
        for chapter in sorted(strong_reasons):
            lines.append(f"- 第{chapter}章：{'；'.join(sorted(strong_reasons[chapter]))}")
    else:
        lines.append("- 未发现事件级直接或传递依赖。")
    lines.extend(["", "## 弱提示（仅共享实体）", ""])
    if weak_reasons:
        for chapter in sorted(weak_reasons):
            lines.append(f"- 第{chapter}章：{'；'.join(sorted(weak_reasons[chapter]))}")
    else:
        lines.append("- 未发现额外的共享实体提示。")
    lines.extend(["", "> 强依赖用于优先修订；弱提示只提醒人工检查，不表示该章一定需要改写。", ""])

    output = output_under(root, "audits", args.output, "rewrite-impact.md")
    atomic_write_text(output, "\n".join(lines).rstrip() + "\n")
    print(json.dumps({
        "strong_affected_chapters": sorted(strong_reasons),
        "weak_affected_chapters": sorted(weak_reasons),
        "output": output.relative_to(root).as_posix(),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
