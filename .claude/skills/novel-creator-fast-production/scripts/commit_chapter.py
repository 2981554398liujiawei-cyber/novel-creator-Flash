#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from chapter_stats import analyze_chapter_text, load_batch_settings, resolve_length_settings
from batch_review import load_review_record, validate_batch_review_record, verify_final_hashes
from batch_state import advance_batch, chapter_in_batch, is_batch_end, load_active_batch

from common import (
    ARC_ID_RE,
    SCENE_BRIDGE_FIELDS,
    append_jsonl_no_follow,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    baseline_path,
    build_baseline_record,
    chapter_filename,
    chapter_meta_filename,
    deep_merge,
    entity_path,
    event_entity_ids,
    extract_entity_ids,
    ensure_no_symlink_chain,
    file_prefix_sha256,
    first_heading,
    heading_ids,
    immutable_fields_for,
    load_events,
    load_json,
    normalize_arc_id,
    normalize_entity_id,
    normalize_event_id,
    normalize_knowledge_used,
    normalize_state_used,
    read_text,
    remove_transaction_backup,
    require_int,
    safe_workspace_path,
    sha256_text,
    title_chapter_number,
    truncate_no_follow,
    utc_timestamp,
    validate_baseline_record,
    validate_current_entity_lists,
    validate_entity_data,
    validate_scene_bridge,
    validate_reader_review,
    validate_chapter_function_fields,
    validate_event_collection,
    validate_transaction_journal,
    validate_workspace_layout,
    workspace_lock,
)


def validate_delta_shape(delta: dict[str, Any], chapter: int, prose: str) -> list[str]:
    errors: list[str] = []
    if delta.get("schema") != 1 or isinstance(delta.get("schema"), bool):
        errors.append("delta schema must be integer 1")
    if delta.get("chapter") != chapter or isinstance(delta.get("chapter"), bool):
        errors.append("delta chapter does not match requested chapter")
    detected = title_chapter_number(prose)
    if detected != chapter:
        errors.append(f"draft title chapter number must be {chapter}; detected {detected}")
    if not first_heading(prose):
        errors.append("draft must begin with a Markdown chapter heading")
    if not str(delta.get("title", "")).strip():
        errors.append("delta title is required")
    if not str(delta.get("summary", "")).strip():
        errors.append("delta summary is required")
    if not isinstance(delta.get("entities", []), list):
        errors.append("entities must be a list")
    if not isinstance(delta.get("entity_changes", []), list):
        errors.append("entity_changes must be a list")
    if not isinstance(delta.get("events", []), list):
        errors.append("events must be a list")
    if not isinstance(delta.get("depends_on_events", []), list):
        errors.append("depends_on_events must be a list")
    errors.extend(validate_chapter_function_fields(delta, field="delta"))
    current_patch = delta.get("current_patch", {})
    if not isinstance(current_patch, dict):
        errors.append("current_patch must be an object")
    else:
        required_fields = ("current_location", "point_of_view", "scene_entities", "current_goal", "scene_bridge")
        for field in required_fields:
            if field not in current_patch:
                errors.append(f"current_patch.{field} is required for every new chapter")
        for field in ("current_location", "point_of_view", "current_goal"):
            if field in current_patch and not isinstance(current_patch.get(field), str):
                errors.append(f"current_patch.{field} must be a string")
        for field in ("scene_entities", "arc_entities", "active_entities"):
            if field in current_patch:
                errors.extend(validate_current_entity_lists(current_patch.get(field), field=f"current_patch.{field}"))
        if "scene_bridge" in current_patch:
            bridge = current_patch.get("scene_bridge")
            errors.extend(validate_scene_bridge(bridge, field="current_patch.scene_bridge"))
            if isinstance(bridge, dict):
                for key in SCENE_BRIDGE_FIELDS:
                    if key not in bridge:
                        errors.append(f"current_patch.scene_bridge.{key} is required")
        if "reader_review" in current_patch:
            errors.extend(validate_reader_review(current_patch.get("reader_review"), field="current_patch.reader_review"))
    return errors


def backup_file(root: Path, backup_root: Path, path: Path) -> dict[str, Any]:
    validate_workspace_layout(root)
    safe_workspace_path(root, path.relative_to(root).as_posix(), allow_missing=True)
    relative = path.relative_to(root).as_posix()
    record = {"path": relative, "existed": path.exists()}
    if path.exists():
        if not path.is_file():
            raise ValueError(f"transaction target must be a regular file: {relative}")
        destination = backup_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Revalidate the source and backup destination after directory creation so a
        # concurrently inserted link cannot redirect the copy outside either tree.
        safe_workspace_path(root, relative, allow_missing=False)
        ensure_no_symlink_chain(backup_root, root, allow_missing=False)
        ensure_no_symlink_chain(destination, backup_root, allow_missing=True)
        shutil.copy2(path, destination, follow_symlinks=False)
        ensure_no_symlink_chain(destination, backup_root, allow_missing=False)
    return record


def restore_transaction(root: Path, journal: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        _, records = validate_transaction_journal(root, journal, require_backups=True)
    except ValueError as exc:
        return [f"invalid transaction journal: {exc}"]
    for record in records:
        target: Path = record["target"]
        source: Path = record["source"]
        try:
            if record["existed"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(target, source.read_bytes())
            else:
                if target.is_dir():
                    errors.append(f"refusing to delete unexpected directory target: {record['path']}")
                else:
                    target.unlink(missing_ok=True)
        except Exception as exc:  # pragma: no cover - recovery path
            errors.append(f"{record['path']}: {exc}")
    try:
        truncate_no_follow(
            root / "state" / "events" / "events.jsonl",
            require_int(journal.get("events_size"), "transaction.events_size", minimum=0),
            root,
            prefix_sha256=str(journal.get("events_prefix_sha256", "")),
        )
    except Exception as exc:  # pragma: no cover - recovery path
        errors.append(f"events rollback: {exc}")
    return errors


def derived_entity_ids(
    delta: dict[str, Any],
    events: list[dict[str, Any]],
    knowledge_used: dict[str, list[dict[str, Any]]],
    state_used: list[dict[str, Any]],
) -> set[str]:
    result: set[str] = set()
    for item in delta.get("entities", []):
        result.add(normalize_entity_id(str(item)))
    for change in delta.get("entity_changes", []):
        if isinstance(change, dict):
            result.add(normalize_entity_id(str(change.get("id", ""))))
            result.update(extract_entity_ids(json.dumps(change.get("patch", {}), ensure_ascii=False)))
    for event in events:
        result.update(event_entity_ids(event))
    result.update(knowledge_used)
    result.update(item["entity_id"] for item in state_used)
    result.update(extract_entity_ids(json.dumps(delta.get("current_patch", {}), ensure_ascii=False)))
    return result


def _existing_baseline_or_error(root: Path, entity_id: str, entity: dict[str, Any]) -> dict[str, Any]:
    path = baseline_path(root, entity_id)
    baseline = load_json(path, default=None)
    if baseline is None:
        raise ValueError(
            f"sealed baseline is missing for {entity_id}; run seal_baselines.py after reviewing the imported project"
        )
    problems = validate_baseline_record(baseline, entity)
    if problems:
        raise ValueError(f"invalid sealed baseline for {entity_id}: " + "; ".join(problems))
    return baseline



def prerequisite_problem(prerequisite: Any, states: dict[str, dict[str, Any]], seen_events: set[str]) -> str | None:
    if isinstance(prerequisite, str):
        try:
            entity_id = normalize_entity_id(prerequisite)
        except ValueError:
            return f"legacy prerequisite is not an entity id: {prerequisite}"
        return None if entity_id in states else f"missing prerequisite entity: {entity_id}"
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


def state_before_chapter(entity: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": entity.get("status", entity.get("initial_status", "")),
        "owner": entity.get("owner", entity.get("initial_owner", "")),
    }

def tracked_state(entity_id: str, entity: dict[str, Any], *, initial: bool = False) -> dict[str, Any]:
    prefix = entity_id.split("-", 1)[0]
    if prefix == "CHAR":
        return {
            "status": entity.get("initial_status" if initial else "status", ""),
            "current_location": entity.get("initial_location" if initial else "current_location", ""),
            "knowledge": set(str(item) for item in entity.get("initial_knowledge" if initial else "knowledge", [])),
            "skills": set(str(item) for item in entity.get("initial_skills" if initial else "skills", [])),
        }
    if prefix == "ITEM":
        return {
            "status": entity.get("initial_status" if initial else "status", ""),
            "owner": entity.get("initial_owner" if initial else "owner", ""),
        }
    if prefix == "REL":
        return {
            "status": entity.get("initial_status" if initial else "status", ""),
            "stage": entity.get("initial_stage" if initial else "stage", ""),
        }
    return {"status": entity.get("initial_status" if initial else "status", "")}


def apply_tracked_event(state: dict[str, Any], row: dict[str, Any], entity_id: str) -> None:
    event_type = str(row.get("type", ""))
    if entity_id.startswith("CHAR-") and str(row.get("character_id", "")).upper() == entity_id:
        if event_type == "knowledge_gained":
            state.setdefault("knowledge", set()).add(str(row.get("fact_id", "")))
        elif event_type == "knowledge_lost":
            state.setdefault("knowledge", set()).discard(str(row.get("fact_id", "")))
        elif event_type == "skill_gained":
            state.setdefault("skills", set()).add(str(row.get("skill", "")))
        elif event_type == "skill_lost":
            state.setdefault("skills", set()).discard(str(row.get("skill", "")))
        elif event_type == "character_moved":
            state["current_location"] = str(row.get("location_id", ""))
        elif event_type == "character_status_changed":
            state["status"] = str(row.get("status", ""))
    elif entity_id.startswith("ITEM-") and str(row.get("item_id", "")).upper() == entity_id:
        if event_type == "item_acquired":
            state.update(status="held", owner=str(row.get("owner", "")))
        elif event_type == "item_transfer":
            state.update(status="held", owner=str(row.get("to", "")))
        elif event_type in {"item_consumed", "item_destroyed", "item_lost"}:
            state.update(status=event_type.removeprefix("item_"), owner="")
    elif entity_id.startswith("QUEST-") and str(row.get("quest_id", "")).upper() == entity_id:
        if event_type == "quest_opened":
            state["status"] = "open"
        elif event_type == "quest_status_changed":
            state["status"] = str(row.get("status", ""))
    elif entity_id.startswith("REL-") and str(row.get("relationship_id", "")).upper() == entity_id and event_type == "relationship_changed":
        if row.get("status") is not None:
            state["status"] = str(row.get("status"))
        if row.get("stage") is not None:
            state["stage"] = str(row.get("stage"))
    elif entity_id.startswith("LOC-") and str(row.get("location_id", "")).upper() == entity_id and event_type == "location_status_changed":
        state["status"] = str(row.get("status", ""))
    elif entity_id.startswith("FS-") and str(row.get("foreshadow_id", "")).upper() == entity_id and event_type == "foreshadow_status_changed":
        state["status"] = str(row.get("status", ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote one staged chapter and its structured state delta as one recoverable operation.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--draft", default="")
    parser.add_argument("--delta", default="")
    parser.add_argument("--arc-update", default="", help="Optional updated current-arc Markdown under drafts/")
    parser.add_argument("--keep-draft", action="store_true")
    parser.add_argument("--min-chars", type=int, help="Override the project minimum effective prose chars")
    parser.add_argument("--target-chars", type=int, help="Override the project target effective prose chars")
    parser.add_argument("--soft-max-chars", type=int, help="Override the project soft maximum effective prose chars")
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    validate_workspace_layout(root)
    chapter = args.chapter
    if chapter < 1:
        parser.error("chapter must be positive")
    final_path = safe_workspace_path(root, f"chapters/{chapter_filename(chapter)}", allow_missing=True)
    staging_path = safe_workspace_path(root, f".novel/staging/{chapter_filename(chapter)}", allow_missing=True)
    if args.draft:
        try:
            draft_path = safe_workspace_path(root, args.draft, allow_missing=False)
        except ValueError as exc:
            parser.error(str(exc))
    elif staging_path.is_file():
        # Writer writes exactly one technical staging file. The chapter becomes formal
        # only after this transaction applies prose, metadata, events, and state together.
        draft_path = staging_path
    else:
        legacy_draft = safe_workspace_path(root, f"drafts/{chapter_filename(chapter)}", allow_missing=True)
        if not legacy_draft.is_file():
            parser.error(
                f"chapter prose is missing; expected {staging_path.relative_to(root)} "
                f"or legacy {legacy_draft.relative_to(root)}"
            )
        draft_path = legacy_draft
    try:
        delta_path = safe_workspace_path(
            root, args.delta or f"state/deltas/{chapter_meta_filename(chapter)}", allow_missing=False
        )
    except ValueError as exc:
        parser.error(str(exc))
    arc_update_path = safe_workspace_path(root, args.arc_update, allow_missing=False) if args.arc_update else None
    if arc_update_path is not None:
        try:
            arc_update_path.relative_to(root / "drafts")
        except ValueError:
            parser.error("--arc-update must be under drafts/")
    journal_path = safe_workspace_path(root, ".novel/transaction.json", allow_missing=True)

    with workspace_lock(root):
        validate_workspace_layout(root)
        if journal_path.exists():
            parser.error("an unfinished chapter transaction exists; run recover_project.py before committing")

        if not draft_path.is_file():
            parser.error(f"chapter input must be a regular file: {draft_path.relative_to(root)}")
        if getattr(draft_path.stat(), "st_nlink", 1) != 1:
            parser.error(f"hard-linked chapter input is not allowed: {draft_path.relative_to(root)}")
        prose_raw = read_text(draft_path, required=True)
        prose = prose_raw.rstrip() + "\n"
        draft_sha = sha256_text(prose_raw)
        delta = load_json(delta_path, required=True)
        if not isinstance(delta, dict):
            parser.error("delta must be a JSON object")
        shape_errors = validate_delta_shape(delta, chapter, prose)
        if shape_errors:
            parser.error("; ".join(shape_errors))
        try:
            length_settings = resolve_length_settings(
                root,
                minimum=args.min_chars,
                target=args.target_chars,
                soft_maximum=args.soft_max_chars,
            )
        except ValueError as exc:
            parser.error(str(exc))
        length_stats = analyze_chapter_text(prose, length_settings)
        if not length_stats["passes_minimum"]:
            parser.error(
                "chapter is too short: "
                f"effective_chars={length_stats['effective_chars']}, "
                f"minimum={length_stats['minimum_effective_chars']}, "
                f"shortfall={length_stats['shortfall']}; "
                "expand the existing staging chapter, rerun chapter-stats, then commit again"
            )
        try:
            knowledge_used = normalize_knowledge_used(delta.get("knowledge_used", {}))
            state_used = normalize_state_used(delta.get("state_used", []))
        except ValueError as exc:
            parser.error(str(exc))

        current_path = safe_workspace_path(root, "state/current.json", allow_missing=False)
        current = load_json(current_path, required=True)
        if not isinstance(current, dict):
            parser.error("state/current.json must be an object")
        latest_value = current.get("latest_chapter", 0)
        if not isinstance(latest_value, int) or isinstance(latest_value, bool) or latest_value < 0:
            parser.error("state/current.json latest_chapter must be a non-negative integer")
        latest = latest_value
        if chapter != latest + 1:
            parser.error(f"chapters commit sequentially; expected chapter {latest + 1}, got {chapter}")
        try:
            batch_settings = load_batch_settings(root)
            active_batch = load_active_batch(root, current)
        except ValueError as exc:
            parser.error(str(exc))
        if not chapter_in_batch(chapter, active_batch):
            parser.error(
                f"chapter {chapter} is outside active batch "
                f"{active_batch['start_chapter']}-{active_batch['end_chapter']}"
            )
        review_path, review_record = load_review_record(root, active_batch)
        if not isinstance(review_record, dict):
            parser.error(
                "every chapter in the active batch requires a finalized double-review record before commit; "
                f"run prepare-review and finalize-review: {review_path.relative_to(root).as_posix()}"
            )
        review_errors = validate_batch_review_record(review_record, active_batch, require_finalized=True)
        review_errors.extend(verify_final_hashes(root, review_record, active_batch))
        if review_errors:
            parser.error("batch review record is not ready: " + "; ".join(review_errors))
        if is_batch_end(chapter, active_batch):
            reader_errors = validate_reader_review(
                delta.get("current_patch", {}).get("reader_review"),
                field="current_patch.reader_review",
                expected_chapter=chapter,
                expected_batch=active_batch,
            )
            if reader_errors:
                parser.error("batch-end chapter requires a complete First Reader conclusion: " + "; ".join(reader_errors))
            reader_review = delta.get("current_patch", {}).get("reader_review", {})
            first_reader = review_record.get("first_reader", {})
            for key in ("verdict", "ending_pull", "revision_applied", "issue_tags", "highest_value_revision"):
                default = [] if key == "issue_tags" else ""
                if reader_review.get(key, default) != first_reader.get(key, default):
                    parser.error(f"current_patch.reader_review.{key} must match finalized batch review record")
        meta_path = safe_workspace_path(root, f"state/chapters/{chapter_meta_filename(chapter)}", allow_missing=True)
        if meta_path.exists() or final_path.exists():
            parser.error(f"chapter {chapter} already has committed output")

        events_path = safe_workspace_path(root, "state/events/events.jsonl", allow_missing=False)
        existing_events = load_events(events_path, required=True)
        existing_event_map: dict[str, dict[str, Any]] = {}
        for event in existing_events:
            try:
                event_id = normalize_event_id(str(event.get("id", "")))
            except ValueError as exc:
                parser.error(str(exc))
            if event_id in existing_event_map:
                parser.error(f"duplicate event id in events.jsonl: {event_id}")
            existing_event_map[event_id] = event

        dependencies: list[str] = []
        for value in delta.get("depends_on_events", []):
            try:
                event_id = normalize_event_id(str(value))
            except ValueError as exc:
                parser.error(str(exc))
            row = existing_event_map.get(event_id)
            if row is None:
                parser.error(f"depends_on_events references missing event: {event_id}")
            if not isinstance(row.get("chapter"), int) or isinstance(row.get("chapter"), bool) or row["chapter"] >= chapter:
                parser.error(f"event dependency must come from an earlier chapter: {event_id}")
            dependencies.append(event_id)
        dependencies = sorted(set(dependencies))

        # Stage entity changes before validating events so newly created entities may be referenced.
        entity_writes: list[tuple[Path, dict[str, Any]]] = []
        baseline_writes: list[tuple[Path, dict[str, Any]]] = []
        staged_entities: dict[str, dict[str, Any]] = {}
        entity_before: dict[str, dict[str, Any] | None] = {}
        changed_ids: set[str] = set()
        for change in delta.get("entity_changes", []):
            if not isinstance(change, dict):
                parser.error("entity_changes entries must be objects")
            try:
                entity_id = normalize_entity_id(str(change.get("id", "")))
            except ValueError as exc:
                parser.error(str(exc))
            if entity_id in changed_ids:
                parser.error(f"duplicate entity change: {entity_id}")
            changed_ids.add(entity_id)
            patch = change.get("patch", {})
            if not isinstance(patch, dict):
                parser.error(f"entity change patch must be an object: {entity_id}")
            path = entity_path(root, entity_id)
            existing = load_json(path, default=None)
            creating = existing is None
            entity_before[entity_id] = existing if isinstance(existing, dict) else None
            if creating and not bool(change.get("create")):
                parser.error(f"entity does not exist and create is false: {entity_id}")
            if not creating and bool(change.get("create")):
                parser.error(f"entity already exists: {entity_id}")
            if creating:
                base: dict[str, Any] = {"schema": 1, "id": entity_id}
                merged = deep_merge(base, patch)
                merged["schema"] = 1
                merged["id"] = entity_id
                # Program-owned creation time cannot be overridden by templates or patches.
                merged["created_chapter"] = chapter
                baseline = build_baseline_record(merged)
                baseline_target = baseline_path(root, entity_id)
                if baseline_target.exists():
                    parser.error(f"baseline already exists for new entity: {entity_id}")
                baseline_writes.append((baseline_target, baseline))
            else:
                if not isinstance(existing, dict):
                    parser.error(f"entity file must be an object: {entity_id}")
                try:
                    _existing_baseline_or_error(root, entity_id, existing)
                except ValueError as exc:
                    parser.error(str(exc))
                forbidden = immutable_fields_for(entity_id) & set(patch)
                if forbidden:
                    parser.error(
                        f"ordinary chapter patches cannot change immutable history for {entity_id}: {', '.join(sorted(forbidden))}"
                    )
                merged = deep_merge(existing, patch)
                merged["schema"] = 1
                merged["id"] = entity_id
            entity_errors = validate_entity_data(merged, entity_id)
            if entity_errors:
                parser.error(f"invalid entity {entity_id}: " + "; ".join(entity_errors))
            staged_entities[entity_id] = merged
            entity_writes.append((path, merged))

        # Resolve all explicitly mentioned entities before event schema validation.
        candidate_ids: set[str] = set(changed_ids)
        for raw in delta.get("entities", []):
            try:
                candidate_ids.add(normalize_entity_id(str(raw)))
            except ValueError as exc:
                parser.error(str(exc))
        candidate_ids.update(knowledge_used)
        candidate_ids.update(item["entity_id"] for item in state_used)
        candidate_ids.update(extract_entity_ids(json.dumps(delta.get("current_patch", {}), ensure_ascii=False)))
        for change in delta.get("entity_changes", []):
            if isinstance(change, dict):
                candidate_ids.update(extract_entity_ids(json.dumps(change.get("patch", {}), ensure_ascii=False)))
        for raw_event in delta.get("events", []):
            if isinstance(raw_event, dict):
                candidate_ids.update(event_entity_ids(raw_event))
        known_entities: set[str] = set()
        for entity_id in candidate_ids:
            if entity_id in staged_entities or entity_path(root, entity_id).is_file():
                known_entities.add(entity_id)
            else:
                parser.error(f"chapter references missing entity: {entity_id}")

        events, event_errors = validate_event_collection(delta.get("events", []), chapter, known_entities)
        if event_errors:
            parser.error("; ".join(event_errors))
        new_event_ids: set[str] = set()
        for row in events:
            event_id = row["id"]
            if event_id in existing_event_map or event_id in new_event_ids:
                parser.error(f"event id must be new and unique: {event_id}")
            new_event_ids.add(event_id)
            row.setdefault("recorded_at", utc_timestamp())
            row["entities"] = sorted(event_entity_ids(row))

        # Every state-changing event must have a staged subject entity. This prevents a
        # commit from succeeding with an event log update while leaving the entity file stale.
        subject_fields = ("character_id", "item_id", "quest_id", "relationship_id", "location_id", "foreshadow_id")
        for row in events:
            if row.get("type") == "note":
                continue
            subject = next((str(row.get(field, "")).upper() for field in subject_fields if row.get(field)), "")
            if not subject or subject not in staged_entities:
                parser.error(f"state-changing event {row['id']} requires a matching entity_changes patch for its subject")

        # Tracked current state must be explainable by this chapter's events, rather than
        # being silently patched in entity JSON.
        for entity_id, merged in staged_entities.items():
            before = entity_before.get(entity_id)
            expected = tracked_state(entity_id, before, initial=False) if before is not None else tracked_state(entity_id, merged, initial=True)
            for row in sorted(events, key=lambda item: item["sequence"]):
                apply_tracked_event(expected, row, entity_id)
            actual = tracked_state(entity_id, merged, initial=False)
            if expected != actual:
                parser.error(f"tracked state change for {entity_id} is not fully represented by chapter events")

        # Validate task prerequisites at the historical moment a task becomes open/active.
        historical_states: dict[str, dict[str, Any]] = {}
        for entity_id in known_entities:
            if entity_id in staged_entities and not entity_path(root, entity_id).exists():
                entity = staged_entities[entity_id]
                historical_states[entity_id] = {
                    "status": entity.get("initial_status", entity.get("status", "")),
                    "owner": entity.get("initial_owner", entity.get("owner", "")),
                }
            else:
                entity = load_json(entity_path(root, entity_id), default={}) or {}
                historical_states[entity_id] = state_before_chapter(entity)
        seen_events = set(existing_event_map)
        for entity_id, entity in staged_entities.items():
            if entity_id.startswith("QUEST-") and not entity_path(root, entity_id).exists():
                if str(entity.get("initial_status", "")) in {"open", "active"}:
                    for prerequisite in entity.get("prerequisites", []):
                        problem = prerequisite_problem(prerequisite, historical_states, seen_events)
                        if problem:
                            parser.error(f"quest {entity_id} prerequisites are not met at creation: {problem}")
        for row in sorted(events, key=lambda item: item["sequence"]):
            event_type = str(row.get("type", ""))
            quest_id = str(row.get("quest_id", "")).upper()
            next_status = "open" if event_type == "quest_opened" else str(row.get("status", ""))
            if event_type in {"quest_opened", "quest_status_changed"} and next_status in {"open", "active"}:
                quest = staged_entities.get(quest_id) or load_json(entity_path(root, quest_id), default={}) or {}
                for prerequisite in quest.get("prerequisites", []):
                    problem = prerequisite_problem(prerequisite, historical_states, seen_events)
                    if problem:
                        parser.error(f"quest {quest_id} prerequisites are not met at event {row['id']}: {problem}")
            item_id = str(row.get("item_id", "")).upper()
            if event_type == "item_acquired" and item_id in historical_states:
                historical_states[item_id].update(status="held", owner=str(row.get("owner", "")))
            elif event_type == "item_transfer" and item_id in historical_states:
                historical_states[item_id].update(status="held", owner=str(row.get("to", "")))
            elif event_type in {"item_consumed", "item_destroyed", "item_lost"} and item_id in historical_states:
                historical_states[item_id].update(status=event_type.removeprefix("item_"), owner="")
            if event_type in {"quest_opened", "quest_status_changed"} and quest_id in historical_states:
                historical_states[quest_id]["status"] = next_status
            seen_events.add(row["id"])

        entity_ids = derived_entity_ids(delta, events, knowledge_used, state_used)
        for entity_id in sorted(entity_ids):
            if entity_id not in staged_entities and not entity_path(root, entity_id).is_file():
                parser.error(f"chapter references missing entity: {entity_id}")

        current_patch = delta.get("current_patch", {})
        next_current = deep_merge(current, current_patch)
        next_current.update({
            "schema": 1,
            "latest_chapter": chapter,
            "outline_node": delta.get("outline_node") or next_current.get("outline_node", ""),
            "recent_summary": delta.get("summary"),
            "last_commit": {"chapter": chapter, "at": utc_timestamp()},
        })
        if is_batch_end(chapter, active_batch):
            next_current["batch"] = advance_batch(active_batch, next_batch_size=batch_settings["batch_size"])
        else:
            next_current["batch"] = active_batch
        for field in ("scene_entities", "arc_entities", "active_entities"):
            if field in next_current:
                problems = validate_current_entity_lists(next_current.get(field), field=f"state/current.json.{field}")
                if problems:
                    parser.error("; ".join(problems))
        if "scene_bridge" in next_current:
            problems = validate_scene_bridge(next_current.get("scene_bridge"), field="state/current.json.scene_bridge")
            if problems:
                parser.error("; ".join(problems))

        arc_update_text: str | None = None
        arc_update_sha: str | None = None
        current_arc_target = safe_workspace_path(root, "plot/current-arc.md", allow_missing=False)
        if arc_update_path is not None:
            arc_raw = read_text(arc_update_path, required=True)
            arc_update_text = arc_raw.rstrip() + "\n"
            arc_update_sha = sha256_text(arc_raw)
            try:
                current_arc_id = normalize_arc_id(str(next_current.get("current_arc", "")))
            except ValueError as exc:
                parser.error(str(exc))
            if not any(current_arc_id in heading_ids(line, ARC_ID_RE) for line in arc_update_text.splitlines()):
                parser.error(f"arc update must have a heading containing exact id {current_arc_id}")

        normalized_delta = dict(delta)
        normalized_delta["events"] = events
        normalized_delta["depends_on_events"] = dependencies
        normalized_delta["knowledge_used"] = knowledge_used
        normalized_delta["state_used"] = state_used
        canonical_delta = json.dumps(normalized_delta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        baseline_hashes = {
            entity_id: build_baseline_record(staged_entities[entity_id])["fields_sha256"]
            for entity_id in sorted(staged_entities)
            if baseline_path(root, entity_id).exists() or any(path.name == f"{entity_id}.json" for path, _ in baseline_writes)
        }
        meta = {
            "schema": 1,
            "chapter": chapter,
            "title": delta.get("title"),
            "chapter_heading": first_heading(prose),
            "summary": delta.get("summary"),
            "outline_node": delta.get("outline_node"),
            "chapter_function": delta.get("chapter_function", ""),
            "dominant_change": delta.get("dominant_change", ""),
            "reader_expectation_added": delta.get("reader_expectation_added", ""),
            "reader_review": current_patch.get("reader_review") if isinstance(current_patch, dict) else None,
            "batch_id": active_batch["batch_id"],
            "batch_start_chapter": active_batch["start_chapter"],
            "batch_end_chapter": active_batch["end_chapter"],
            "batch_review_record": review_path.relative_to(root).as_posix(),
            "entities": sorted(entity_ids),
            "events": [row["id"] for row in events],
            "depends_on_events": dependencies,
            "knowledge_used": knowledge_used,
            "state_used": state_used,
            "current_location": next_current.get("current_location", ""),
            "point_of_view": next_current.get("point_of_view", ""),
            "scene_entities": next_current.get("scene_entities", []),
            "current_goal": next_current.get("current_goal", ""),
            "scene_bridge": next_current.get("scene_bridge", {}),
            "length": {
                "metric": "letters_and_digits_excluding_heading_whitespace_and_punctuation",
                "effective_chars": length_stats["effective_chars"],
                "non_whitespace_chars": length_stats["non_whitespace_chars"],
                "paragraphs": length_stats["paragraphs"],
                "minimum_effective_chars": length_stats["minimum_effective_chars"],
                "target_effective_chars": length_stats["target_effective_chars"],
                "soft_maximum_effective_chars": length_stats["soft_maximum_effective_chars"],
                "status": length_stats["status"],
            },
            "baseline_hashes": baseline_hashes,
            "prose_sha256": sha256_text(prose),
            "delta_sha256": sha256_text(canonical_delta),
            "revision": 1,
            "revision_history": [],
            "prose_rewrite_review_required": False,
            "committed_at": utc_timestamp(),
        }

        transaction_id = f"commit-{chapter:04d}-{uuid.uuid4().hex[:10]}"
        backup_root = safe_workspace_path(root, f".novel/backups/{transaction_id}", allow_missing=True)
        touched = [current_path, final_path, meta_path, delta_path]
        touched.extend(path for path, _ in entity_writes)
        touched.extend(path for path, _ in baseline_writes)
        if arc_update_text is not None:
            touched.append(current_arc_target)
        events_size = events_path.stat().st_size
        journal = {
            "schema": 1,
            "transaction_id": transaction_id,
            "status": "applying",
            "chapter": chapter,
            "created_at": utc_timestamp(),
            "backup_dir": backup_root.relative_to(root).as_posix(),
            "events_size": events_size,
            "events_prefix_sha256": file_prefix_sha256(events_path, events_size),
            "files": [],
        }
        backup_root.mkdir(parents=True, exist_ok=False)
        for path in touched:
            journal["files"].append(backup_file(root, backup_root, path))
        atomic_write_json(journal_path, journal)
        validate_transaction_journal(root, journal, require_backups=True)

        try:
            atomic_write_json(current_path, next_current)
            for path, data in entity_writes:
                atomic_write_json(path, data)
            for path, data in baseline_writes:
                atomic_write_json(path, data)
            atomic_write_json(delta_path, normalized_delta)
            atomic_write_json(meta_path, meta)
            if arc_update_text is not None:
                atomic_write_text(current_arc_target, arc_update_text)
            append_jsonl_no_follow(events_path, events, root)
            atomic_write_text(final_path, prose)
        except Exception:
            rollback_errors = restore_transaction(root, journal)
            if rollback_errors:
                journal["status"] = "dirty"
                journal["rollback_errors"] = rollback_errors
                atomic_write_json(journal_path, journal)
            else:
                journal_path.unlink(missing_ok=True)
                try:
                    remove_transaction_backup(root, backup_root, ignore_missing=True)
                except (OSError, ValueError):
                    pass
            raise

        journal["status"] = "committed"
        journal["committed_at"] = utc_timestamp()
        atomic_write_json(journal_path, journal)
        # The transaction marker is removed before best-effort garbage collection so cleanup
        # failure cannot permanently block the next chapter.
        journal_path.unlink()
        try:
            remove_transaction_backup(root, backup_root, ignore_missing=True)
        except (OSError, ValueError):
            pass

        if (
            not args.keep_draft
            and draft_path.resolve(strict=False) != final_path.resolve(strict=False)
            and draft_path.exists()
            and sha256_text(read_text(draft_path, required=True)) == draft_sha
        ):
            draft_path.unlink()
        if not args.keep_draft and arc_update_path is not None and arc_update_path.exists() and arc_update_sha is not None:
            if sha256_text(read_text(arc_update_path, required=True)) == arc_update_sha:
                arc_update_path.unlink()
        print(json.dumps({
            "committed": True,
            "chapter": chapter,
            "chapter_file": final_path.relative_to(root).as_posix(),
            "metadata": meta_path.relative_to(root).as_posix(),
            "events": len(events),
            "entities_indexed": len(entity_ids),
            "entities_changed": len(entity_writes),
            "baselines_created": len(baseline_writes),
            "length": meta["length"],
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
