#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from common import (
    ARC_ID_RE,
    ENTITY_PREFIX_TO_DIR,
    NODE_ID_RE,
    arc_summary_path,
    atomic_write_text,
    baseline_path,
    chapter_meta_filename,
    event_entity_ids,
    first_heading,
    heading_ids,
    list_chapters,
    load_events,
    load_json,
    markdown_intro_and_exact_section,
    normalize_arc_id,
    normalize_entity_id,
    normalize_event_id,
    normalize_knowledge_used,
    normalize_node_id,
    normalize_state_used,
    output_under,
    read_text,
    require_int,
    sha256_text,
    split_markdown_frontmatter,
    title_chapter_number,
    validate_baseline_record,
    validate_current_entity_lists,
    validate_entity_data,
    validate_event_payload,
    validate_scene_bridge,
    validate_reader_review,
    validate_chapter_function_fields,
    validate_transaction_journal,
    validate_workspace_layout,
    validate_revision_history,
    ensure_no_symlink_chain,
)
from chapter_stats import load_batch_settings, load_length_settings
from production_state import load_production_settings
from batch_review import chapter_key, current_prose_hash, validate_batch_review_record
from batch_state import load_active_batch, load_active_review_unit, validate_batch, validate_review_unit
from commit_chapter import derived_entity_ids
from reader_model import load_reader_model, validate_model as validate_reader_model

VALID_STATUSES = {
    "characters": {"active", "inactive", "dead", "missing", "unknown"},
    "locations": {"active", "inactive", "destroyed", "sealed", "unknown"},
    "items": {"available", "held", "consumed", "destroyed", "lost", "inactive", "unknown"},
    "quests": {"planned", "open", "active", "paused", "completed", "failed", "cancelled", "unknown"},
    "foreshadows": {"open", "touched", "resolved", "abandoned", "deferred", "unknown"},
    "relationships": {"active", "broken", "resolved", "inactive", "unknown"},
}


def entity_files(root: Path) -> tuple[dict[str, tuple[str, Path, dict[str, Any]]], list[str]]:
    result: dict[str, tuple[str, Path, dict[str, Any]]] = {}
    errors: list[str] = []
    for kind in VALID_STATUSES:
        directory = root / "state" / "entities" / kind
        ensure_no_symlink_chain(directory, root, allow_missing=True)
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                ensure_no_symlink_chain(path, root, allow_missing=False)
                data = load_json(path, required=True)
            except (ValueError, json.JSONDecodeError, FileNotFoundError) as exc:
                errors.append(f"实体文件不可用：{path.relative_to(root)} → {exc}")
                continue
            if not isinstance(data, dict):
                errors.append(f"实体文件必须是对象：{path.relative_to(root)}")
                continue
            file_id = path.stem.upper()
            entity_id = str(data.get("id") or file_id).upper()
            try:
                normalized_file_id = normalize_entity_id(file_id)
                normalized_entity_id = normalize_entity_id(entity_id)
            except ValueError as exc:
                errors.append(f"实体文件名或 ID 非法：{path.relative_to(root)} → {exc}")
                continue
            if normalized_file_id != normalized_entity_id:
                errors.append(f"实体文件名与内部 ID 不一致：{path.relative_to(root)} → {normalized_entity_id}")
                continue
            entity_id = normalized_entity_id
            if entity_id in result:
                errors.append(f"重复实体 ID：{entity_id} → {result[entity_id][1].relative_to(root)} / {path.relative_to(root)}")
            else:
                result[entity_id] = (kind, path, data)
    return result, errors


def initial_state_for(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind == "characters":
        return {
            "status": data.get("initial_status", "unknown"),
            "current_location": data.get("initial_location", ""),
            "knowledge": set(str(item) for item in data.get("initial_knowledge", [])),
            "skills": set(str(item) for item in data.get("initial_skills", [])),
        }
    if kind == "items":
        return {"status": data.get("initial_status", "unknown"), "owner": data.get("initial_owner", "")}
    if kind == "quests":
        return {"status": data.get("initial_status", "planned")}
    if kind == "relationships":
        return {"status": data.get("initial_status", "active"), "stage": data.get("initial_stage", "")}
    if kind == "locations":
        return {"status": data.get("initial_status", "active")}
    if kind == "foreshadows":
        return {"status": data.get("initial_status", "open")}
    return {"status": data.get("status", "unknown")}


def apply_event(states: dict[str, dict[str, Any]], row: dict[str, Any]) -> None:
    event_type = str(row.get("type", ""))
    character_id = str(row.get("character_id", "")).upper()
    if event_type == "knowledge_gained" and character_id in states:
        states[character_id].setdefault("knowledge", set()).add(str(row.get("fact_id", "")))
    elif event_type == "knowledge_lost" and character_id in states:
        states[character_id].setdefault("knowledge", set()).discard(str(row.get("fact_id", "")))
    elif event_type == "character_moved" and character_id in states:
        states[character_id]["current_location"] = str(row.get("location_id", ""))
    elif event_type == "character_status_changed" and character_id in states:
        states[character_id]["status"] = str(row.get("status", ""))
    elif event_type == "skill_gained" and character_id in states:
        states[character_id].setdefault("skills", set()).add(str(row.get("skill", "")))
    elif event_type == "skill_lost" and character_id in states:
        states[character_id].setdefault("skills", set()).discard(str(row.get("skill", "")))

    item_id = str(row.get("item_id", "")).upper()
    if item_id in states:
        if event_type == "item_acquired":
            states[item_id].update(owner=str(row.get("owner", "")), status="held")
        elif event_type == "item_transfer":
            states[item_id].update(owner=str(row.get("to", "")), status="held")
        elif event_type in {"item_consumed", "item_destroyed", "item_lost"}:
            states[item_id].update(owner="", status=event_type.removeprefix("item_"))

    quest_id = str(row.get("quest_id", "")).upper()
    if event_type == "quest_opened" and quest_id in states:
        states[quest_id]["status"] = "open"
    elif event_type == "quest_status_changed" and quest_id in states:
        states[quest_id]["status"] = str(row.get("status", ""))

    relationship_id = str(row.get("relationship_id", "")).upper()
    if event_type == "relationship_changed" and relationship_id in states:
        if row.get("status") is not None:
            states[relationship_id]["status"] = str(row.get("status"))
        if row.get("stage") is not None:
            states[relationship_id]["stage"] = str(row.get("stage"))

    location_id = str(row.get("location_id", "")).upper()
    if event_type == "location_status_changed" and location_id in states:
        states[location_id]["status"] = str(row.get("status", ""))

    foreshadow_id = str(row.get("foreshadow_id", "")).upper()
    if event_type == "foreshadow_status_changed" and foreshadow_id in states:
        states[foreshadow_id]["status"] = str(row.get("status", ""))


def transition_problem(states: dict[str, dict[str, Any]], row: dict[str, Any]) -> str | None:
    event_type = str(row.get("type", ""))
    if event_type == "item_transfer":
        item_id = str(row.get("item_id", "")).upper()
        expected_from = str(row.get("from", ""))
        if item_id in states and str(states[item_id].get("owner", "")) != expected_from:
            return f"物品转让来源冲突：{item_id} 事件 {row.get('id')} 期望 {expected_from}，事件链当前为 {states[item_id].get('owner', '')}"
    return None


def compare_assertion(state: dict[str, Any], assertion: dict[str, Any]) -> bool:
    field = str(assertion.get("field", ""))
    expected = assertion.get("equals")
    actual = state.get(field)
    if isinstance(actual, set):
        if isinstance(expected, list):
            return actual == set(str(item) for item in expected)
        return str(expected) in actual
    return actual == expected


def prerequisite_problem(prerequisite: Any, states: dict[str, dict[str, Any]], seen_events: set[str]) -> str | None:
    if isinstance(prerequisite, str):
        try:
            entity_id = normalize_entity_id(prerequisite)
        except ValueError:
            return f"legacy prerequisite is not an entity id: {prerequisite}"
        return None if entity_id in states else f"missing prerequisite entity at this time: {entity_id}"
    if not isinstance(prerequisite, dict):
        return "prerequisite must be a string or object"
    kind = str(prerequisite.get("type", ""))
    value = str(prerequisite.get("id", ""))
    if kind == "event_exists":
        try:
            event_id = normalize_event_id(value)
        except ValueError as exc:
            return str(exc)
        return None if event_id in seen_events else f"missing prerequisite event at this time: {event_id}"
    try:
        entity_id = normalize_entity_id(value)
    except ValueError as exc:
        return str(exc)
    if entity_id not in states:
        return f"missing prerequisite entity at this time: {entity_id}"
    state = states[entity_id]
    if kind == "entity_exists":
        return None
    if kind in {"entity_status", "quest_status"}:
        expected = str(prerequisite.get("equals", ""))
        return None if str(state.get("status", "")) == expected else f"{entity_id} status is {state.get('status')}, expected {expected}"
    if kind == "item_owner":
        expected = str(prerequisite.get("equals", ""))
        return None if str(state.get("owner", "")) == expected else f"{entity_id} owner is {state.get('owner')}, expected {expected}"
    return f"unknown prerequisite type: {kind}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic structure, event-history, and reference checks. This does not replace literary review.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--output", default="latest-audit.md")
    parser.add_argument("--allow-gaps", action="store_true")
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    errors: list[str] = []
    warnings: list[str] = []

    # Custom Claude Code subagents automatically load project CLAUDE.md memory.
    # Warn when project-level memory appears to contain story answers that can pollute blind reading.
    blind_memory_keywords = ("幕后", "真凶", "结局", "预期转折", "人物秘密", "盲读标准", "master-outline", "current-arc")
    for memory_rel in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
        memory_path = root / memory_rel
        if memory_path.is_file():
            try:
                memory_text = memory_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hits = [key for key in blind_memory_keywords if key in memory_text]
            if hits:
                warnings.append(
                    f"盲读污染风险：{memory_rel} 含疑似剧情答案/审读提示（{', '.join(hits[:4])}）；自定义 Subagent 会自动加载 CLAUDE.md 层级"
                )
    try:
        validate_workspace_layout(root)
    except ValueError as exc:
        print(json.dumps({"clean": False, "errors": 1, "warnings": 0, "output": "", "reason": f"工作区路径布局不安全：{exc}"}, ensure_ascii=False))
        return 2

    try:
        load_length_settings(root)
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(f"章节篇幅设置无效：{exc}")
    try:
        batch_settings = load_batch_settings(root)
    except (ValueError, json.JSONDecodeError) as exc:
        batch_settings = {"batch_size": 5, "planning_window": 10}
        errors.append(f"批次设置无效：{exc}")
    try:
        production_settings = load_production_settings(root)
    except (ValueError, json.JSONDecodeError) as exc:
        production_settings = {"writer_pool_size": 5, "blind_reader_count": 1}
        errors.append(f"快速生产设置无效：{exc}")

    journal_path = root / ".novel" / "transaction.json"
    if journal_path.exists():
        errors.append("存在未完成的章节事务；先运行 recover_project.py。")
        try:
            journal = load_json(journal_path, required=True)
            status = str(journal.get("status", "")) if isinstance(journal, dict) else ""
            validate_transaction_journal(root, journal, require_backups=status in {"applying", "dirty"})
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append(f"事务日志结构不安全或损坏：{exc}")

    try:
        chapters = list_chapters(root)
    except ValueError as exc:
        chapters = []
        errors.append(f"章节目录不安全：{exc}")
    numbers = [number for number, _ in chapters]
    if numbers and not args.allow_gaps:
        missing = sorted(set(range(1, numbers[-1] + 1)) - set(numbers))
        if missing:
            errors.append("章节号存在空洞：" + ", ".join(map(str, missing[:30])))

    try:
        names = load_json(root / "canon" / "names.json", default={}) or {}
    except json.JSONDecodeError as exc:
        names = {}
        errors.append(f"canon/names.json 不是合法 JSON：{exc}")
    forbidden_raw = names.get("forbidden_terms", []) if isinstance(names, dict) else []
    if not isinstance(forbidden_raw, list):
        errors.append("canon/names.json forbidden_terms 必须是数组")
        forbidden_raw = []
    forbidden = [str(item) for item in forbidden_raw if isinstance(item, str) and item.strip()]

    entities, entity_load_errors = entity_files(root)
    errors.extend(entity_load_errors)
    for entity_id, (kind, path, data) in entities.items():
        try:
            normalized = normalize_entity_id(entity_id)
        except ValueError as exc:
            errors.append(f"{path.relative_to(root)}：{exc}")
            continue
        expected_kind = ENTITY_PREFIX_TO_DIR.get(normalized.split("-", 1)[0])
        if expected_kind != kind:
            errors.append(f"实体目录与 ID 类型不一致：{normalized} 位于 {kind}")
        errors.extend(f"实体 {normalized}：{item}" for item in validate_entity_data(data, normalized))
        status = str(data.get("status", ""))
        if status not in VALID_STATUSES[kind]:
            errors.append(f"实体 {normalized} status 非法：{status}")
        baseline = load_json(baseline_path(root, normalized), default=None)
        if baseline is None:
            errors.append(f"实体 {normalized} 缺少不可变基线；先运行 seal_baselines.py 并人工复核")
        else:
            errors.extend(f"实体 {normalized}：{item}" for item in validate_baseline_record(baseline, data))

    known_entity_ids = set(entities)
    parsed_events: list[dict[str, Any]] = []
    event_map: dict[str, dict[str, Any]] = {}
    sequences_by_chapter: dict[int, set[int]] = defaultdict(set)
    try:
        raw_events = load_events(root / "state" / "events" / "events.jsonl", required=True)
    except (ValueError, FileNotFoundError) as exc:
        raw_events = []
        errors.append(f"事件日志不可用：{exc}")
    for index, row in enumerate(raw_events, 1):
        chapter = row.get("chapter")
        if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 1:
            errors.append(f"事件日志第{index}行 chapter 必须是正整数")
            continue
        event_errors = validate_event_payload(row, chapter, known_entity_ids)
        if event_errors:
            errors.extend(f"事件日志第{index}行：{item}" for item in event_errors)
            continue
        event_id = normalize_event_id(str(row.get("id", "")))
        sequence = row["sequence"]
        if event_id in event_map:
            errors.append(f"重复事件 ID：{event_id}")
            continue
        if sequence in sequences_by_chapter[chapter]:
            errors.append(f"第{chapter}章事件 sequence 重复：{sequence}")
            continue
        sequences_by_chapter[chapter].add(sequence)
        event_map[event_id] = row
        parsed_events.append(row)
    parsed_events.sort(key=lambda row: (row["chapter"], row["sequence"], row["id"]))

    chapter_metas: dict[int, dict[str, Any]] = {}
    chapter_deltas: dict[int, dict[str, Any]] = {}
    all_meta_event_ids: set[str] = set()
    legacy_batch_metadata_chapters: list[int] = []
    for number, path in chapters:
        text = read_text(path)
        if not text.strip():
            errors.append(f"空章节：{path.name}")
            continue
        if title_chapter_number(text) != number:
            errors.append(f"标题章号与文件名不一致：{path.name}")
        for term in forbidden:
            if term in text:
                errors.append(f"正文含禁用词：{path.name} → {term}")
        meta_path = root / "state" / "chapters" / chapter_meta_filename(number)
        delta_path = root / "state" / "deltas" / chapter_meta_filename(number)
        try:
            ensure_no_symlink_chain(meta_path, root, allow_missing=False)
            ensure_no_symlink_chain(delta_path, root, allow_missing=False)
            meta = load_json(meta_path, required=True)
            delta = load_json(delta_path, required=True)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"第{number}章元数据或增量不可用：{exc}")
            continue
        if not isinstance(meta, dict) or not isinstance(delta, dict):
            errors.append(f"第{number}章元数据与增量必须是对象")
            continue
        chapter_metas[number] = meta
        chapter_deltas[number] = delta
        try:
            meta_chapter = require_int(meta.get("chapter"), f"第{number}章元数据 chapter", minimum=1)
        except ValueError as exc:
            errors.append(str(exc))
            meta_chapter = None
        if meta_chapter is not None and meta_chapter != number:
            errors.append(f"章节元数据章号错误：第{number}章")
        try:
            delta_chapter = require_int(delta.get("chapter"), f"第{number}章增量 chapter", minimum=1)
        except ValueError as exc:
            errors.append(str(exc))
            delta_chapter = None
        if delta_chapter is not None and delta_chapter != number:
            errors.append(f"章节状态增量章号错误：第{number}章")
        errors.extend(f"第{number}章：{item}" for item in validate_chapter_function_fields(delta, field="delta"))
        for key in ("chapter_function", "dominant_change", "reader_expectation_added"):
            if meta.get(key, "") != delta.get(key, ""):
                errors.append(f"第{number}章 {key} 与状态增量不一致")
        batch_meta = {
            "review_kind": meta.get("review_kind", "batch"),
            "batch_id": meta.get("batch_id"),
            "start_chapter": meta.get("batch_start_chapter"),
            "end_chapter": meta.get("batch_end_chapter"),
        }
        batch_values = [batch_meta[key] for key in ("batch_id", "start_chapter", "end_chapter")]
        if all(value is None for value in batch_values):
            batch_meta = None
            legacy_batch_metadata_chapters.append(number)
        elif all(
            isinstance(batch_meta[key], int) and not isinstance(batch_meta[key], bool)
            for key in ("batch_id", "start_chapter", "end_chapter")
        ):
            batch_meta["batch_size"] = batch_meta["end_chapter"] - batch_meta["start_chapter"] + 1
            batch_meta["next_review_chapter"] = batch_meta["end_chapter"]
            errors.extend(f"第{number}章：{item}" for item in validate_review_unit(batch_meta, field="chapter review unit"))
            if not (batch_meta["start_chapter"] <= number <= batch_meta["end_chapter"]):
                errors.append(f"第{number}章不在其记录的批次范围内")
        else:
            batch_meta = None
            errors.append(f"第{number}章批次锚点元数据不完整")
        delta_review = delta.get("current_patch", {}).get("reader_review") if isinstance(delta.get("current_patch"), dict) else None
        if delta_review is not None:
            expected_batch = batch_meta if isinstance(batch_meta, dict) and number == batch_meta["end_chapter"] else None
            errors.extend(
                f"第{number}章：{item}"
                for item in validate_reader_review(
                    delta_review,
                    field="current_patch.reader_review",
                    expected_chapter=number,
                    expected_batch=expected_batch,
                )
            )
        if isinstance(batch_meta, dict) and number == batch_meta["end_chapter"]:
            if delta_review is None or (isinstance(delta_review, dict) and delta_review.get("reason") != "batch"):
                errors.append(f"第{number}章作为批次末章缺少 batch reader_review")
        elif isinstance(delta_review, dict) and delta_review.get("reason") == "batch":
            errors.append(f"第{number}章不是其历史批次末章，却记录了 batch reader_review")
        if meta.get("reader_review") != delta_review:
            errors.append(f"第{number}章 reader_review 与状态增量不一致")
        if str(meta.get("prose_sha256", "")) != sha256_text(text):
            errors.append(f"章节正文与元数据哈希不一致：第{number}章")
        if meta.get("chapter_heading") and str(meta.get("chapter_heading")) != first_heading(text):
            errors.append(f"章节标题与元数据不一致：第{number}章")
        errors.extend(validate_revision_history(root, number, meta))
        if meta.get("prose_rewrite_review_required"):
            errors.append(f"第{number}章重写后尚未确认结构化事实未变化")

        try:
            normalized_knowledge = normalize_knowledge_used(delta.get("knowledge_used", {}))
            normalized_state = normalize_state_used(delta.get("state_used", []))
        except ValueError as exc:
            errors.append(f"第{number}章增量 schema 错误：{exc}")
            normalized_knowledge, normalized_state = {}, []
        if meta.get("knowledge_used", {}) != normalized_knowledge:
            errors.append(f"第{number}章 knowledge_used 与规范化增量不一致")
        if meta.get("state_used", []) != normalized_state:
            errors.append(f"第{number}章 state_used 与规范化增量不一致")
        canonical_delta = json.dumps(delta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if str(meta.get("delta_sha256", "")) != sha256_text(canonical_delta):
            errors.append(f"章节状态增量与提交记录不一致：第{number}章")

        delta_events = [item for item in delta.get("events", []) if isinstance(item, dict)]
        try:
            expected_entities = derived_entity_ids(delta, delta_events, normalized_knowledge, normalized_state)
        except ValueError as exc:
            expected_entities = set()
            errors.append(f"第{number}章实体索引无法派生：{exc}")
        meta_entities = meta.get("entities", [])
        if not isinstance(meta_entities, list):
            errors.append(f"第{number}章元数据 entities 必须是数组")
            meta_entities = []
        actual_entities = {str(item).upper() for item in meta_entities}
        if expected_entities != actual_entities:
            errors.append(f"第{number}章实体索引与状态增量不一致：缺少={sorted(expected_entities-actual_entities)} 多余={sorted(actual_entities-expected_entities)}")
        for entity_id in actual_entities:
            if entity_id not in entities:
                errors.append(f"第{number}章引用不存在的实体：{entity_id}")
            else:
                created_value = entities[entity_id][2].get("created_chapter")
                if isinstance(created_value, int) and not isinstance(created_value, bool) and created_value > number:
                    errors.append(f"第{number}章提前引用尚未创建的实体：{entity_id}")

        expected_event_ids = [str(event.get("id", "")).upper() for event in delta_events]
        meta_events = meta.get("events", [])
        if not isinstance(meta_events, list):
            errors.append(f"第{number}章元数据 events 必须是数组")
            meta_events = []
        if expected_event_ids != [str(item).upper() for item in meta_events]:
            errors.append(f"第{number}章事件索引与状态增量不一致")
        for event_id_raw in meta_events:
            try:
                event_id = normalize_event_id(str(event_id_raw))
            except ValueError as exc:
                errors.append(f"第{number}章：{exc}")
                continue
            all_meta_event_ids.add(event_id)
            row = event_map.get(event_id)
            if row is None:
                errors.append(f"第{number}章引用不存在的事件：{event_id}")
            elif row["chapter"] != number:
                errors.append(f"事件 {event_id} 的 chapter 与章节元数据不一致")
        meta_dependencies = meta.get("depends_on_events", [])
        if not isinstance(meta_dependencies, list):
            errors.append(f"第{number}章元数据 depends_on_events 必须是数组")
            meta_dependencies = []
        for dep_raw in meta_dependencies:
            try:
                dep = normalize_event_id(str(dep_raw))
            except ValueError as exc:
                errors.append(f"第{number}章：{exc}")
                continue
            row = event_map.get(dep)
            if row is None:
                errors.append(f"第{number}章依赖不存在的事件：{dep}")
            elif row["chapter"] >= number:
                errors.append(f"第{number}章依赖未来或同章事件：{dep}")

        baseline_hashes = meta.get("baseline_hashes", {})
        if not isinstance(baseline_hashes, dict):
            errors.append(f"第{number}章 baseline_hashes 必须是对象")
        else:
            for entity_id, (_, _, data) in entities.items():
                if data.get("created_chapter") == number and not isinstance(data.get("created_chapter"), bool):
                    baseline = load_json(baseline_path(root, entity_id), default={}) or {}
                    if str(baseline_hashes.get(entity_id, "")) != str(baseline.get("fields_sha256", "")):
                        errors.append(f"第{number}章缺少创建实体的基线哈希：{entity_id}")

        for field in ("current_location", "point_of_view", "current_goal"):
            if field in meta and not isinstance(meta.get(field), str):
                errors.append(f"第{number}章 {field} 必须是字符串")
        if "scene_entities" in meta:
            errors.extend(validate_current_entity_lists(meta.get("scene_entities"), field=f"第{number}章 scene_entities"))
        if "scene_bridge" in meta:
            for problem in validate_scene_bridge(meta.get("scene_bridge"), field=f"第{number}章 scene_bridge"):
                errors.append(problem)

    if legacy_batch_metadata_chapters:
        preview = ", ".join(str(number) for number in legacy_batch_metadata_chapters[:8])
        suffix = "……" if len(legacy_batch_metadata_chapters) > 8 else ""
        warnings.append(
            f"共有{len(legacy_batch_metadata_chapters)}章使用旧版元数据，未保存历史批次锚点"
            f"（章节：{preview}{suffix}）；不追溯套用当前批次设置"
        )

    reviewed_records: set[str] = set()
    for number, meta in chapter_metas.items():
        record_rel = meta.get("batch_review_record")
        batch_end_number = meta.get("batch_end_chapter")
        if not isinstance(record_rel, str) or not record_rel:
            if number == batch_end_number:
                errors.append(f"第{number}章缺少 batch_review_record")
            continue
        record_path = root / record_rel
        if not record_path.exists():
            if number == batch_end_number:
                errors.append(f"批次末章缺少审读记录：{record_rel}")
            continue
        if record_rel in reviewed_records:
            continue
        reviewed_records.add(record_rel)
        try:
            ensure_no_symlink_chain(record_path, root, allow_missing=False)
            record = load_json(record_path, required=True)
        except (FileNotFoundError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"批次审读记录不可用：{record_rel} → {exc}")
            continue
        batch = {
            "review_kind": meta.get("review_kind", "batch"),
            "batch_id": meta.get("batch_id"),
            "start_chapter": meta.get("batch_start_chapter"),
            "end_chapter": meta.get("batch_end_chapter"),
            "batch_size": int(meta.get("batch_end_chapter", 0)) - int(meta.get("batch_start_chapter", 0)) + 1
                if isinstance(meta.get("batch_start_chapter"), int) and isinstance(meta.get("batch_end_chapter"), int) else 0,
            "next_review_chapter": meta.get("batch_end_chapter"),
        }
        record_errors = validate_batch_review_record(record, batch, require_finalized=True)
        final_hashes = record.get("final_hashes", {}) if isinstance(record, dict) else {}
        for review_chapter in range(batch.get("start_chapter", 0), batch.get("end_chapter", -1) + 1):
            key = chapter_key(review_chapter)
            try:
                actual_hash = current_prose_hash(root, review_chapter)
            except ValueError as exc:
                record_errors.append(str(exc))
                continue
            expected_hash = final_hashes.get(key) if isinstance(final_hashes, dict) else None
            if expected_hash == actual_hash:
                continue
            rewritten_meta = chapter_metas.get(review_chapter, {})
            rewrite_review = rewritten_meta.get("rewrite_review", {}) if isinstance(rewritten_meta, dict) else {}
            rewrite_confirmed = (
                isinstance(rewrite_review, dict)
                and rewrite_review.get("status") == "confirmed"
                and rewritten_meta.get("prose_rewrite_review_required") is False
                and rewritten_meta.get("prose_sha256") == actual_hash
            )
            if rewrite_confirmed:
                warnings.append(
                    f"第{review_chapter}章正文在批次审读后完成了已确认的独立重写；"
                    "批次审读哈希保留为历史快照"
                )
            else:
                record_errors.append(
                    f"batch review final hash no longer matches {key}; "
                    "rerun finalize-review after edits"
                )
        errors.extend(f"批次审读记录 {record_rel}：{item}" for item in record_errors)
        end_meta = chapter_metas.get(batch.get("end_chapter", -1), {})
        end_review = end_meta.get("reader_review") if isinstance(end_meta, dict) else None
        first_reader = record.get("first_reader", {}) if isinstance(record, dict) else {}
        if isinstance(end_review, dict) and isinstance(first_reader, dict):
            mapping = {
                "verdict": "verdict",
                "ending_pull": "ending_pull",
                "revision_applied": "revision_applied",
                "issue_tags": "issue_tags",
                "highest_value_revision": "highest_value_revision",
            }
            for review_key, record_key in mapping.items():
                if end_review.get(review_key, [] if review_key == "issue_tags" else "") != first_reader.get(record_key, [] if record_key == "issue_tags" else ""):
                    errors.append(f"批次审读记录 {record_rel} 与末章 reader_review.{review_key} 不一致")

    recent_numbers = sorted(chapter_metas)[-5:]
    if recent_numbers:
        if latest_number := recent_numbers[-1]:
            recent = [chapter_metas[number] for number in recent_numbers]
            if len(recent) >= 2 and all(not str(meta.get("point_of_view", "")).strip() for meta in recent[-2:]):
                warnings.append("最近连续2章 point_of_view 为空；请确认视角并非无意丢失")
            if len(recent) >= 2 and all(not str(meta.get("scene_bridge", {}).get("last_action", "")).strip() for meta in recent[-2:] if isinstance(meta.get("scene_bridge"), dict)):
                warnings.append("最近连续2章 scene_bridge.last_action 为空；跨章动作承接可能不足")
            if str(recent[-1].get("current_location", "")).strip() == "" and latest_number > 0:
                warnings.append("最新章节 current_location 为空；若场景具有明确地点，请补充承接状态")
            latest_bridge = recent[-1].get("scene_bridge", {})
            latest_is_batch_end = recent[-1].get("batch_end_chapter") == latest_number
            if isinstance(latest_bridge, dict) and not latest_is_batch_end:
                if not str(latest_bridge.get("immediate_pressure", "")).strip() and not str(latest_bridge.get("emotional_residue", "")).strip():
                    warnings.append("最新非批次末章的 immediate_pressure 与 emotional_residue 均为空；请确认并非遗漏余波")
            if len(recent) == 5:
                signatures = []
                for meta in recent:
                    signatures.append((
                        meta.get("current_location", ""),
                        meta.get("point_of_view", ""),
                        tuple(meta.get("scene_entities", [])) if isinstance(meta.get("scene_entities"), list) else (),
                        meta.get("current_goal", ""),
                        json.dumps(meta.get("scene_bridge", {}), ensure_ascii=False, sort_keys=True),
                    ))
                if len(set(signatures)) == 1:
                    warnings.append("最近5章承接状态完全不变；请确认状态维护没有停滞")

    recent_functions = [
        str(chapter_metas[number].get("chapter_function", ""))
        for number in sorted(chapter_metas)[-8:]
        if str(chapter_metas[number].get("chapter_function", ""))
    ]
    if len(recent_functions) >= 6:
        counts = {name: recent_functions.count(name) for name in set(recent_functions)}
        dominant_name, dominant_count = max(counts.items(), key=lambda item: item[1])
        if dominant_count >= 7:
            warnings.append(f"最近章节功能较单一：最近{len(recent_functions)}章中有{dominant_count}章为 {dominant_name}；仅作节奏复盘提示")
        if len(recent_functions) >= 6 and all(name == "build" for name in recent_functions[-6:]):
            warnings.append("最近6章均标记为 build；请确认蓄势是否已经形成具体回报或升级压力")

    orphan_events = set(event_map) - all_meta_event_ids
    if orphan_events:
        warnings.append("事件日志中存在未被章节元数据列出的事件：" + ", ".join(sorted(orphan_events)[:30]))

    try:
        current = load_json(root / "state" / "current.json", default={}) or {}
    except (json.JSONDecodeError, UnicodeError) as exc:
        current = {}
        errors.append(f"state/current.json 不可用：{exc}")
    if not isinstance(current, dict):
        errors.append("state/current.json 必须是对象")
        current = {}
    try:
        reader_model = load_reader_model(root)
        reader_errors = validate_reader_model(reader_model)
        errors.extend("Reader Model：" + item for item in reader_errors)
        if not (root / "state" / "reader-model.json").is_file():
            warnings.append("旧项目尚无 state/reader-model.json；系统会在下一次 commit 时自动建立")
    except (ValueError, json.JSONDecodeError, UnicodeError) as exc:
        errors.append(f"state/reader-model.json 不可用：{exc}")
    latest = numbers[-1] if numbers else 0
    latest_value = current.get("latest_chapter", 0) if isinstance(current, dict) else None
    if not isinstance(latest_value, int) or isinstance(latest_value, bool):
        errors.append("current.json latest_chapter 必须是非负整数")
    elif latest_value != latest:
        errors.append(f"current.json latest_chapter={latest_value}，实际最新提交章={latest}")
    for field, label in (
        ("active_entities", "活跃实体"),
        ("scene_entities", "场景实体"),
        ("arc_entities", "故事弧实体"),
    ):
        values = current.get(field, []) if isinstance(current, dict) else []
        problems = validate_current_entity_lists(values, field=f"current.json {field}")
        errors.extend(problems)
        if problems:
            values = []
        for entity_id_raw in values:
            try:
                entity_id = normalize_entity_id(str(entity_id_raw))
                if entity_id not in entities:
                    errors.append(f"current.json {label}不存在：{entity_id}")
            except ValueError as exc:
                errors.append(str(exc))
    for key in ("current_location", "point_of_view", "current_goal"):
        if key in current and not isinstance(current.get(key), str):
            errors.append(f"current.json {key} 必须是字符串")
    if "scene_bridge" in current:
        errors.extend(validate_scene_bridge(current.get("scene_bridge"), field="current.json scene_bridge"))
    reader_review = current.get("reader_review", {})
    reviewed_through = reader_review.get("reviewed_through_chapter", 0) if isinstance(reader_review, dict) else 0
    if isinstance(reviewed_through, int) and not isinstance(reviewed_through, bool) and reviewed_through > 0:
        errors.extend(validate_reader_review(reader_review, field="current.json reader_review"))
        if reviewed_through > latest:
            errors.append("current.json reader_review.reviewed_through_chapter 不能晚于最新提交章")
        if reader_review.get("reason") == "batch":
            reviewed_meta = chapter_metas.get(reviewed_through, {})
            if reviewed_meta.get("batch_end_chapter") is not None:
                if reviewed_meta.get("batch_end_chapter") != reviewed_through:
                    errors.append("current.json batch reader_review 必须对应其历史批次末章")
                for key, meta_key in (("batch_id", "batch_id"), ("batch_start_chapter", "batch_start_chapter"), ("batch_end_chapter", "batch_end_chapter")):
                    if reader_review.get(key) != reviewed_meta.get(meta_key):
                        errors.append(f"current.json reader_review.{key} 与历史章节批次元数据不一致")
    elif not isinstance(reader_review, dict):
        errors.append("current.json reader_review 必须是对象")
    elif reviewed_through == 0:
        if any(reader_review.get(key) not in ("", None, []) for key in (
            "reason", "verdict", "ending_pull", "revision_applied", "issue_tags", "highest_value_revision"
        )):
            errors.append("current.json 未开始盲读时 reader_review 其余字段必须为空")
    completed_review_ends = [
        number
        for number, meta in chapter_metas.items()
        if meta.get("batch_end_chapter") == number
        and isinstance(meta.get("reader_review"), dict)
        and meta.get("reader_review", {}).get("reason") == "batch"
    ]
    latest_required_review = max(completed_review_ends, default=0)
    if latest_required_review and (
        not isinstance(reviewed_through, int) or reviewed_through < latest_required_review
    ):
        warnings.append(
            f"First Reader 盲读节奏已逾期：最近完成批次末章={latest_required_review}，"
            f"current.json 最近记录到={reviewed_through}"
        )
    try:
        active_batch = load_active_batch(root, current)
        errors.extend(validate_batch(active_batch, field="current.json batch"))
        active_review = load_active_review_unit(root, current)
        errors.extend(validate_review_unit(active_review, field="current.json review unit"))
        if latest >= active_review["end_chapter"] and (not isinstance(reviewed_through, int) or reviewed_through < active_review["end_chapter"]):
            warnings.append(
                f"First Reader 盲读节奏已逾期：最新章={latest}，"
                f"当前审读单元={active_review['start_chapter']}-{active_review['end_chapter']}，"
                f"最近记录到={reviewed_through}"
            )
    except ValueError as exc:
        errors.append(f"当前批次状态无效：{exc}")
    if isinstance(reader_review, dict) and reader_review.get("verdict") == "weak" and reader_review.get("revision_applied") is False:
        warnings.append("最近一次 First Reader 评价为 weak，且记录为未采纳修订；请在批次复盘中说明决定")
    for key in ("current_location", "point_of_view"):
        for entity_id in event_entity_ids({"entities": [str(current.get(key, ""))]}):
            if entity_id not in entities:
                errors.append(f"current.json {key} 引用不存在的实体：{entity_id}")

    outline = read_text(root / "plot" / "master-outline.md")
    outline_node_raw = str(current.get("outline_node", ""))
    if outline_node_raw:
        try:
            outline_node = normalize_node_id(outline_node_raw)
            if markdown_intro_and_exact_section(outline, outline_node, NODE_ID_RE) is None:
                errors.append(f"当前总纲节点不存在：{outline_node}")
        except ValueError as exc:
            errors.append(str(exc))
    current_arc_raw = str(current.get("current_arc", ""))
    current_arc_text = read_text(root / "plot" / "current-arc.md")
    if current_arc_raw:
        try:
            current_arc = normalize_arc_id(current_arc_raw)
            if not any(current_arc in heading_ids(line, ARC_ID_RE) for line in current_arc_text.splitlines()):
                errors.append(f"当前故事弧文件没有精确标题：{current_arc}")
        except ValueError as exc:
            errors.append(str(exc))
    relevant_arcs = current.get("relevant_arcs", []) if isinstance(current, dict) else []
    if not isinstance(relevant_arcs, list):
        errors.append("current.json relevant_arcs 必须是数组")
        relevant_arcs = []
    for arc_raw in relevant_arcs:
        try:
            arc_id = normalize_arc_id(str(arc_raw))
            arc_path = arc_summary_path(root, arc_id)
            if arc_path is None:
                errors.append(f"current.json 相关故事弧摘要不存在：{arc_id}")
            else:
                arc_text = read_text(arc_path)
                if not any(arc_id in heading_ids(line, ARC_ID_RE) for line in arc_text.splitlines()):
                    errors.append(f"相关故事弧摘要没有精确标题：{arc_id}")
        except ValueError as exc:
            errors.append(str(exc))

    handoff_text = read_text(root / "state" / "session-handoff.md")
    if handoff_text.strip():
        handoff_meta, _ = split_markdown_frontmatter(handoff_text)
        if handoff_meta:
            schema_value = handoff_meta.get("schema")
            if schema_value != 1 or isinstance(schema_value, bool):
                errors.append("session-handoff.md schema 必须是整数 1")
            through = handoff_meta.get("through_chapter")
            if not isinstance(through, int) or isinstance(through, bool) or through < 0:
                errors.append("session-handoff.md through_chapter 必须是非负整数")
            else:
                if through > latest:
                    errors.append(f"session-handoff.md through_chapter={through} 超过当前已提交章 {latest}")
                elif latest - through > 8:
                    warnings.append(f"会话交接已过期：仅覆盖到第 {through} 章，当前第 {latest} 章")
            handoff_arc = str(handoff_meta.get("current_arc", "")).strip().upper()
            if handoff_arc and current_arc_raw and handoff_arc != current_arc_raw.upper():
                warnings.append(f"会话交接故事弧不匹配：{handoff_arc}，当前 {current_arc_raw.upper()}")
        else:
            warnings.append("session-handoff.md 使用旧格式；建议补充 schema、through_chapter 和 current_arc")

    # Historical replay. Entities appear only at their sealed created_chapter.
    created_by_chapter: dict[int, list[str]] = defaultdict(list)
    for entity_id, (_, _, data) in entities.items():
        created = data.get("created_chapter")
        if isinstance(created, int) and not isinstance(created, bool) and created > 0:
            created_by_chapter[created].append(entity_id)
    events_by_chapter: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in parsed_events:
        events_by_chapter[row["chapter"]].append(row)
    states: dict[str, dict[str, Any]] = {}
    seen_events: set[str] = set()

    for chapter in sorted(chapter_metas):
        for entity_id in sorted(created_by_chapter.get(chapter, [])):
            kind, _, data = entities[entity_id]
            states[entity_id] = initial_state_for(kind, data)
            if kind == "quests" and states[entity_id].get("status") in {"open", "active"}:
                for prerequisite in data.get("prerequisites", []):
                    problem = prerequisite_problem(prerequisite, states, seen_events)
                    if problem:
                        errors.append(f"任务 {entity_id} 在第{chapter}章开启时前置条件不满足：{problem}")

        meta = chapter_metas[chapter]
        timeline: list[tuple[int, int, str, Any]] = []
        try:
            knowledge_used = normalize_knowledge_used(meta.get("knowledge_used", {}))
            state_used = normalize_state_used(meta.get("state_used", []))
        except ValueError as exc:
            errors.append(f"第{chapter}章使用记录损坏：{exc}")
            knowledge_used, state_used = {}, []
        for character_id, facts in knowledge_used.items():
            for fact in facts:
                timeline.append((fact["sequence"], 0, "knowledge", (character_id, fact["fact_id"])))
        for assertion in state_used:
            timeline.append((assertion["sequence"], 0, "state", assertion))
        for row in events_by_chapter.get(chapter, []):
            timeline.append((row["sequence"], 1, "event", row))
        timeline.sort(key=lambda item: (item[0], item[1], str(item[3])))

        for _, _, kind, payload in timeline:
            if kind == "knowledge":
                character_id, fact_id = payload
                if character_id not in states:
                    errors.append(f"第{chapter}章 knowledge_used 引用当时尚未创建的角色：{character_id}")
                elif fact_id not in states[character_id].get("knowledge", set()):
                    errors.append(f"知识穿帮：第{chapter}章 {character_id} 使用了当时尚未知的 {fact_id}")
            elif kind == "state":
                entity_id = payload["entity_id"]
                if entity_id not in states or not compare_assertion(states[entity_id], payload):
                    errors.append(f"历史状态穿帮：第{chapter}章断言 {entity_id}.{payload.get('field')} 不成立")
            else:
                row = payload
                event_type = str(row.get("type", ""))
                quest_id = str(row.get("quest_id", "")).upper()
                if event_type in {"quest_opened", "quest_status_changed"} and quest_id in entities:
                    next_status = "open" if event_type == "quest_opened" else str(row.get("status", ""))
                    if next_status in {"open", "active"}:
                        for prerequisite in entities[quest_id][2].get("prerequisites", []):
                            problem = prerequisite_problem(prerequisite, states, seen_events)
                            if problem:
                                errors.append(f"任务 {quest_id} 在事件 {row.get('id')} 时前置条件不满足：{problem}")
                problem = transition_problem(states, row)
                if problem:
                    errors.append(problem)
                apply_event(states, row)
                seen_events.add(row["id"])

    # Compare final files with the event-derived state.
    for entity_id, (kind, _, data) in entities.items():
        state = states.get(entity_id)
        if state is None:
            errors.append(f"实体创建章不在已提交历史中：{entity_id}")
            continue
        if kind == "characters":
            if set(str(item) for item in data.get("knowledge", [])) != state.get("knowledge", set()):
                errors.append(f"角色知识与事件链不一致：{entity_id}")
            if set(str(item) for item in data.get("skills", [])) != state.get("skills", set()):
                errors.append(f"角色技能与事件链不一致：{entity_id}")
            if str(data.get("status", "")) != str(state.get("status", "")):
                errors.append(f"角色状态与事件链不一致：{entity_id}")
            if str(data.get("current_location", "")) != str(state.get("current_location", "")):
                errors.append(f"角色位置与事件链不一致：{entity_id}")
        elif kind == "locations" and str(data.get("status", "")) != str(state.get("status", "")):
            errors.append(f"地点状态与事件链不一致：{entity_id}")
        elif kind == "items":
            if str(data.get("status", "")) != str(state.get("status", "")) or str(data.get("owner", "")) != str(state.get("owner", "")):
                errors.append(f"物品状态或持有者与事件链不一致：{entity_id}")
        elif kind in {"quests", "relationships", "foreshadows"} and str(data.get("status", "")) != str(state.get("status", "")):
            errors.append(f"实体状态与事件链不一致：{entity_id}")
        if kind == "relationships" and str(data.get("stage", "")) != str(state.get("stage", "")):
            errors.append(f"关系阶段与事件链不一致：{entity_id}")
        if kind == "foreshadows":
            target = data.get("target_chapter")
            if isinstance(target, int) and not isinstance(target, bool) and target < latest and data.get("status") in {"open", "touched"}:
                warnings.append(f"伏笔逾期未处理：{entity_id} 目标章 {target}，当前第 {latest} 章")

    lines = [
        "# 结构化连续性审计", "",
        "> 检查文件、不可变基线、时间化状态、事件依赖和引用完整性，不替代人物、节奏与文学质量判断。", "",
        f"- 已提交章节：{len(chapters)}",
        f"- 结构化实体：{len(entities)}",
        f"- 事件：{len(parsed_events)}",
        f"- 错误：{len(errors)}",
        f"- 警告：{len(warnings)}", "",
        "## 错误", "",
    ]
    lines.extend([f"- {item}" for item in errors] or ["- 无。"])
    lines.extend(["", "## 警告", ""])
    lines.extend([f"- {item}" for item in warnings] or ["- 无。"])
    try:
        output = output_under(root, "audits", args.output, "latest-audit.md")
        atomic_write_text(output, "\n".join(lines).rstrip() + "\n")
        output_value = output.relative_to(root).as_posix()
    except ValueError as exc:
        errors.append(f"审计报告无法安全写入：{exc}")
        output_value = ""
    print(json.dumps({"clean": not errors, "errors": len(errors), "warnings": len(warnings), "output": output_value}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
