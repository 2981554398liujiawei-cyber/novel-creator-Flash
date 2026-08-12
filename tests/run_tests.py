#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".claude" / "skills" / "novel-creator-flash"
SCRIPTS = SKILL / "scripts"
ASSETS = SKILL / "assets"
sys.path.insert(0, str(SCRIPTS))

import common  # noqa: E402
from common import append_jsonl_no_follow, atomic_write_json, workspace_lock  # noqa: E402
from search_memory import balanced_select, collect  # noqa: E402
from production_state import reader_agents_for  # noqa: E402


def run_script(name: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / name), *map(str, args)],
        text=True,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        timeout=30,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"{name} failed\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def init_project(path: Path, *, relaxed_length: bool = True, test_batch_size: int = 1) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_script("init_project.py", str(path), "--title", "未命名作品", "--genre", "通用类型", check=True)
    # Formal review batches are always five chapters. Tiny regression fixtures
    # use the explicit 1-4 chapter final_tail exception instead of mutating
    # batch_size, so tests exercise the same invariant as production.
    if relaxed_length:
        write_json(path / "state" / "writing-settings.json", {
            "schema": 1,
            "chapter_length": {
                "minimum_effective_chars": 1,
                "target_effective_chars": 1,
                "soft_maximum_effective_chars": 100000,
            },
            "batch": {"batch_size": 5, "planning_window": 10},
            "production": {"writer_pool_size": 5, "blind_reader_count": 1},
        })
    if test_batch_size != 5:
        if test_batch_size not in {1, 2, 3, 4}:
            raise ValueError("test_batch_size must be 1-5")
        current_path = path / "state" / "current.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        batch = current["batch"]
        start_chapter = int(batch["start_chapter"])
        current["final_tail"] = {
            "schema": 1,
            "review_kind": "final_tail",
            "batch_id": int(batch["batch_id"]),
            "start_chapter": start_chapter,
            "end_chapter": start_chapter + test_batch_size - 1,
            "batch_size": test_batch_size,
            "next_review_chapter": start_chapter + test_batch_size - 1,
            "chapter_count": test_batch_size,
            "writer": "main-agent",
        }
        write_json(current_path, current)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def activate_final_tail(path: Path, count: int = 1) -> None:
    if count not in {1, 2, 3, 4}:
        raise ValueError("test final tail count must be 1-4")
    current_path = path / "state/current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    batch = current["batch"]
    start = int(batch["start_chapter"])
    current["final_tail"] = {
        "schema": 1, "review_kind": "final_tail", "batch_id": int(batch["batch_id"]),
        "start_chapter": start, "end_chapter": start + count - 1, "batch_size": count,
        "next_review_chapter": start + count - 1, "chapter_count": count, "writer": "main-agent",
    }
    write_json(current_path, current)


def delta_for(chapter: int, title: str, summary: str) -> dict:
    return {
        "schema": 1,
        "chapter": chapter,
        "title": title,
        "summary": summary,
        "outline_node": "NODE-001",
        "chapter_function": "",
        "dominant_change": "",
        "reader_expectation_added": "",
        "entities": [],
        "events": [],
        "depends_on_events": [],
        "knowledge_used": {},
        "state_used": [],
        "entity_changes": [],
        "current_patch": {
            "current_location": "",
            "point_of_view": "",
            "scene_entities": [],
            "current_goal": "",
            "scene_bridge": {
                "time": "",
                "location": "",
                "pov": "",
                "last_action": "",
                "immediate_pressure": "",
                "emotional_residue": "",
            },
        },
    }


def draft(path: Path, chapter: int, title: str, body: str = "正文内容。") -> None:
    staging = path / ".novel" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / f"chapter-{chapter:04d}.md").write_text(f"# 第{chapter}章 {title}\n\n{body}\n", encoding="utf-8")


def rewrite_draft(path: Path, chapter: int, title: str, body: str) -> None:
    drafts = path / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / f"chapter-{chapter:04d}.md").write_text(
        f"# 第{chapter}章 {title}\n\n{body}\n", encoding="utf-8"
    )


def _active_review_unit(path: Path) -> dict:
    current = json.loads((path / "state/current.json").read_text(encoding="utf-8"))
    return current.get("final_tail") or current.get("batch", {})


def finalize_batch_review_from_delta(path: Path, chapter: int, data: dict) -> None:
    unit = _active_review_unit(path)
    if chapter != unit.get("end_chapter"):
        return
    reader = data.get("current_patch", {}).get("reader_review")
    if not isinstance(reader, dict) or reader.get("reason") != "batch":
        return
    reader.setdefault("issue_tags", [])
    reader.setdefault("highest_value_revision", "")
    reader.setdefault("batch_id", unit.get("batch_id"))
    reader.setdefault("batch_start_chapter", unit.get("start_chapter"))
    reader.setdefault("batch_end_chapter", unit.get("end_chapter"))
    reader.setdefault("review_kind", unit.get("review_kind", "batch"))
    prefix = "final-tail" if unit.get("review_kind") == "final_tail" else "batch"
    record_path = path / "state" / "reviews" / f"{prefix}-{unit['start_chapter']:04d}-{unit['end_chapter']:04d}.json"
    if record_path.is_file():
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        if existing.get("finalized") is True:
            return
    extra = ["--continuity-risk", "low"]
    if unit.get("review_kind") == "final_tail":
        extra += ["--final-tail-count", str(unit["chapter_count"])]
    prepared = run_script("prepare_batch_review.py", str(path), *extra, check=True)
    payload = json.loads(prepared.stdout)
    record_path = path / payload["output"]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    required = int(record.get("first_reader", {}).get("required_count", 1))
    record["first_reader"] = {
        "status": "completed",
        "required_count": required,
        "completed_readers": list(reader_agents_for(required)),
        "available_readers": list(reader_agents_for(required)),
        "verdict": reader.get("verdict"),
        "ending_pull": reader.get("ending_pull"),
        "revision_applied": reader.get("revision_applied"),
        "issue_tags": reader.get("issue_tags", []),
        "highest_value_revision": reader.get("highest_value_revision", ""),
    }
    record["continuity"] = {"status": "completed", "checked_by": "main-agent", "blocking_count": 0, "warning_count": 0}
    write_json(record_path, record)
    run_script("finalize_batch_review.py", str(path), check=True)


def commit(
    path: Path,
    chapter: int,
    data: dict,
    *,
    expect_ok: bool = True,
    prepare_review: bool = True,
) -> subprocess.CompletedProcess[str]:
    if prepare_review:
        unit = _active_review_unit(path)
        if chapter == unit.get("end_chapter") and "reader_review" not in data.get("current_patch", {}):
            data.setdefault("current_patch", {})["reader_review"] = {
                "reviewed_through_chapter": chapter,
                "reason": "batch",
                "verdict": "acceptable",
                "ending_pull": "fair",
                "revision_applied": True,
                "issue_tags": [],
                "highest_value_revision": "",
                "batch_id": unit.get("batch_id"),
                "batch_start_chapter": unit.get("start_chapter"),
                "batch_end_chapter": unit.get("end_chapter"),
                "review_kind": unit.get("review_kind", "batch"),
            }
        try:
            finalize_batch_review_from_delta(path, int(unit.get("end_chapter", chapter)), data)
        except AssertionError:
            if expect_ok:
                raise
    write_json(path / "state" / "deltas" / f"chapter-{chapter:04d}.json", data)
    proc = run_script("commit_chapter.py", str(path), "--chapter", str(chapter))
    if expect_ok and proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc


class NovelSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="novel-skill-test-")
        self.root = Path(self.temp.name) / "book"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_basic_commit_context_audit_export(self) -> None:
        init_project(self.root)
        context = run_script("build_context.py", str(self.root), "--chapter", "1", check=True)
        self.assertTrue(json.loads(context.stdout)["ready"])
        draft(self.root, 1, "起点")
        commit(self.root, 1, delta_for(1, "起点", "故事开始"))
        audit = run_script("audit_project.py", str(self.root))
        self.assertEqual(audit.returncode, 0, audit.stderr)
        export = run_script("export_novel.py", str(self.root), check=True)
        self.assertTrue((self.root / "exports" / "novel.md").is_file())
        self.assertTrue(json.loads(export.stdout)["exported"])

    def test_committed_cleanup_rejects_symlinked_backup_directory(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        init_project(self.root)
        outside = Path(self.temp.name) / "outside-backup-target"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        transaction_id = "commit-0001-bbbbbbbbbb"
        linked = self.root / ".novel" / "backups" / transaction_id
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        write_json(self.root / ".novel" / "transaction.json", {
            "schema": 1,
            "transaction_id": transaction_id,
            "status": "committed",
            "chapter": 1,
            "backup_dir": f".novel/backups/{transaction_id}",
            "events_size": 0,
            "files": [{"path": "state/current.json", "existed": True}],
        })
        proc = run_script("recover_project.py", str(self.root))
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(marker.is_file())
        self.assertTrue(outside.is_dir())
        self.assertTrue((self.root / ".novel/transaction.json").is_file())

    def test_recovery_rejects_path_traversal(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / ".novel").mkdir()
        victim = Path(self.temp.name) / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        outside_dir = Path(self.temp.name) / "outside"
        outside_dir.mkdir()
        (outside_dir / "keep.txt").write_text("keep", encoding="utf-8")
        write_json(self.root / ".novel" / "transaction.json", {
            "schema": 1,
            "transaction_id": "commit-0001-aaaaaaaaaa",
            "status": "dirty",
            "chapter": 1,
            "backup_dir": str(outside_dir),
            "events_size": 0,
            "files": [{"path": "../victim.txt", "existed": False}],
        })
        proc = run_script("recover_project.py", str(self.root))
        self.assertEqual(proc.returncode, 2)
        self.assertTrue(victim.exists())
        self.assertTrue(outside_dir.exists())
        self.assertTrue((self.root / ".novel" / "transaction.json").exists())

    def test_commit_rechecks_transaction_after_lock(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "起点")
        write_json(self.root / "state" / "deltas" / "chapter-0001.json", delta_for(1, "起点", "开始"))
        with workspace_lock(self.root):
            proc = subprocess.Popen(
                [sys.executable, str(SCRIPTS / "commit_chapter.py"), str(self.root), "--chapter", "1"],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            time.sleep(0.4)
            transaction_id = "commit-9999-aaaaaaaaaa"
            backup = self.root / ".novel" / "backups" / transaction_id / "state"
            backup.mkdir(parents=True)
            shutil.copy2(self.root / "state" / "current.json", backup / "current.json")
            atomic_write_json(self.root / ".novel" / "transaction.json", {
                "schema": 1,
                "transaction_id": transaction_id,
                "status": "dirty",
                "chapter": 9999,
                "backup_dir": f".novel/backups/{transaction_id}",
                "events_size": 0,
                "files": [{"path": "state/current.json", "existed": True}],
            })
        stdout, stderr = proc.communicate(timeout=10)
        self.assertNotEqual(proc.returncode, 0, stdout + stderr)
        self.assertTrue((self.root / ".novel" / "transaction.json").exists())
        self.assertFalse((self.root / "chapters" / "chapter-0001.md").exists())

    def test_valid_recovery_restores_allowed_state_file(self) -> None:
        init_project(self.root)
        original = (self.root / "state" / "current.json").read_text(encoding="utf-8")
        transaction_id = "commit-0001-bbbbbbbbbb"
        backup = self.root / ".novel" / "backups" / transaction_id / "state"
        backup.mkdir(parents=True)
        (backup / "current.json").write_text(original, encoding="utf-8")
        (self.root / "state" / "current.json").write_text('{"broken": true}\n', encoding="utf-8")
        write_json(self.root / ".novel" / "transaction.json", {
            "schema": 1, "transaction_id": transaction_id, "status": "dirty", "chapter": 1,
            "backup_dir": f".novel/backups/{transaction_id}", "events_size": 0,
            "files": [{"path": "state/current.json", "existed": True}],
        })
        proc = run_script("recover_project.py", str(self.root), check=True)
        self.assertEqual(json.loads(proc.stdout)["action"], "rolled back")
        self.assertEqual((self.root / "state" / "current.json").read_text(encoding="utf-8"), original)

    def test_arc_update_is_part_of_chapter_transaction(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "起点")
        arc_draft = self.root / "drafts" / "current-arc.md"
        arc_draft.write_text("# ARC-001 当前故事弧\n\n- 本章已经推进。\n", encoding="utf-8")
        data = delta_for(1, "起点", "开始")
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        batch = current.get("final_tail") or current["batch"]
        data["current_patch"]["reader_review"] = {
            "reviewed_through_chapter": 1, "reason": "batch", "verdict": "acceptable",
            "ending_pull": "fair", "revision_applied": True, "issue_tags": [],
            "highest_value_revision": "", "batch_id": batch["batch_id"],
            "batch_start_chapter": batch["start_chapter"], "batch_end_chapter": batch["end_chapter"],
            "review_kind": batch.get("review_kind", "batch"),
        }
        finalize_batch_review_from_delta(self.root, 1, data)
        write_json(self.root / "state" / "deltas" / "chapter-0001.json", data)
        proc = run_script("commit_chapter.py", str(self.root), "--chapter", "1", "--arc-update", "drafts/current-arc.md", check=True)
        self.assertTrue(json.loads(proc.stdout)["committed"])
        self.assertIn("本章已经推进", (self.root / "plot" / "current-arc.md").read_text(encoding="utf-8"))
        self.assertFalse(arc_draft.exists())

    def test_historical_knowledge_is_not_washed_by_later_state(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "角色甲"})
        d1 = delta_for(1, "泄密", "角色提前说出秘密")
        d1["knowledge_used"] = {"CHAR-0001": ["FACT-0001"]}
        d1["entity_changes"] = [{"id": "CHAR-0001", "create": True, "patch": character}]
        draft(self.root, 1, "泄密")
        commit(self.root, 1, d1)
        activate_final_tail(self.root)

        d2 = delta_for(2, "得知", "角色正式得知秘密")
        d2["events"] = [{"type": "knowledge_gained", "character_id": "CHAR-0001", "fact_id": "FACT-0001"}]
        d2["knowledge_used"] = {"CHAR-0001": ["FACT-0001"]}
        d2["entity_changes"] = [{"id": "CHAR-0001", "patch": {"knowledge": ["FACT-0001"]}}]
        draft(self.root, 2, "得知")
        commit(self.root, 2, d2)
        audit = run_script("audit_project.py", str(self.root))
        self.assertEqual(audit.returncode, 1)
        report = (self.root / "audits" / "latest-audit.md").read_text(encoding="utf-8")
        self.assertIn("第1章", report)
        self.assertIn("知识穿帮", report)

    def test_entity_index_is_derived_and_missing_event_dependency_is_rejected(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "角色甲"})
        d1 = delta_for(1, "出现", "实体出现")
        d1["entity_changes"] = [{"id": "CHAR-0001", "create": True, "patch": character}]
        draft(self.root, 1, "出现")
        commit(self.root, 1, d1)
        meta = json.loads((self.root / "state" / "chapters" / "chapter-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["entities"], ["CHAR-0001"])
        activate_final_tail(self.root)

        d2 = delta_for(2, "依赖", "错误依赖")
        d2["depends_on_events"] = ["EVT-9999-999"]
        draft(self.root, 2, "依赖")
        proc = commit(self.root, 2, d2, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing event", proc.stderr)

    def test_required_context_and_exact_node_matching(self) -> None:
        init_project(self.root)
        self.assertFalse((self.root / "plot" / "chapter-plans").exists())
        proc = run_script("build_context.py", str(self.root), "--chapter", "1", "--query", "当前章从村口开始")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ready"])

        (self.root / "plot" / "master-outline.md").write_text(
            "# 全书\n\n## NODE-0010 错误节点\n\n内容\n", encoding="utf-8"
        )
        proc = run_script("build_context.py", str(self.root), "--chapter", "1")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("NODE-001", proc.stdout)

        (self.root / "plot" / "master-outline.md").write_text(
            (ASSETS / "master-outline-template.md").read_text(encoding="utf-8"), encoding="utf-8"
        )
        current = json.loads((self.root / "state" / "current.json").read_text(encoding="utf-8"))
        current["active_entities"] = ["CHAR-999"]
        current["relevant_arcs"] = ["ARC-999"]
        write_json(self.root / "state" / "current.json", current)
        optional = run_script("build_context.py", str(self.root), "--chapter", "1")
        self.assertEqual(optional.returncode, 0, optional.stderr)

        current["scene_entities"] = ["CHAR-999"]
        write_json(self.root / "state" / "current.json", current)
        required = run_script("build_context.py", str(self.root), "--chapter", "1")
        self.assertEqual(required.returncode, 2)
        self.assertIn("CHAR-999", required.stdout)

    def test_current_arc_context_extracts_only_the_selected_arc(self) -> None:
        init_project(self.root)
        (self.root / "plot/current-arc.md").write_text(
            "# ARC-001 当前故事弧\n\nKEEP-CURRENT-ARC\n\n## 当前章\n\n- 继续\n\n"
            "# ARC-002 未来故事弧\n\nDO-NOT-LEAK-FUTURE-ARC\n",
            encoding="utf-8",
        )
        proc = run_script("build_context.py", str(self.root), "--chapter", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        packet = (self.root / json.loads(proc.stdout)["output"]).read_text(encoding="utf-8")
        self.assertIn("KEEP-CURRENT-ARC", packet)
        self.assertNotIn("DO-NOT-LEAK-FUTURE-ARC", packet)

    def test_rewrite_keeps_history_and_blocks_export_until_confirmed(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "起点", "旧正文。")
        commit(self.root, 1, delta_for(1, "起点", "旧版本"))
        rewrite_draft(self.root, 1, "起点", "新正文，可能改变事实。")
        rewrite = run_script("rewrite_prose.py", str(self.root), "--chapter", "1", check=True)
        data = json.loads(rewrite.stdout)
        self.assertTrue(data["review_required"])
        self.assertTrue((self.root / data["archive"]).is_file())
        self.assertNotEqual(run_script("audit_project.py", str(self.root)).returncode, 0)
        self.assertNotEqual(run_script("export_novel.py", str(self.root)).returncode, 0)
        run_script("confirm_rewrite.py", str(self.root), "--chapter", "1", "--note", "已逐项核对，结构化事实未变化", check=True)
        self.assertEqual(run_script("audit_project.py", str(self.root)).returncode, 0)
        self.assertEqual(run_script("export_novel.py", str(self.root)).returncode, 0)

    def test_transitive_rewrite_impact(self) -> None:
        self.root.mkdir(parents=True)
        meta_dir = self.root / "state" / "chapters"
        meta_dir.mkdir(parents=True)
        write_json(meta_dir / "chapter-0001.json", {"chapter": 1, "entities": ["CHAR-001"], "events": ["EVT-0001-001"], "depends_on_events": []})
        write_json(meta_dir / "chapter-0002.json", {"chapter": 2, "entities": ["CHAR-001"], "events": ["EVT-0002-001"], "depends_on_events": ["EVT-0001-001"]})
        write_json(meta_dir / "chapter-0003.json", {"chapter": 3, "entities": [], "events": [], "depends_on_events": ["EVT-0002-001"]})
        proc = run_script("rewrite_impact.py", str(self.root), "--chapter", "1", check=True)
        data = json.loads(proc.stdout)
        self.assertEqual(data["strong_affected_chapters"], [2, 3])

    def test_memory_selection_keeps_each_available_type(self) -> None:
        init_project(self.root)
        # Create one matching record for every type and additional high-scoring records.
        for i in range(1, 5):
            (self.root / "state" / "arc-summaries" / f"ARC-{i:03d}.md").write_text(f"# ARC-{i:03d}\n\n长期线索 既有约定\n", encoding="utf-8")
        (self.root / "canon" / "facts.md").write_text("# 事实\n\n长期线索 既有约定\n", encoding="utf-8")
        (self.root / "canon" / "changes.md").write_text("# 变更\n\n长期线索 既有约定\n", encoding="utf-8")
        entity_dir = self.root / "state" / "entities" / "items"
        write_json(entity_dir / "ITEM-0001.json", {"schema": 1, "id": "ITEM-0001", "name": "长期线索", "created_chapter": 1, "initial_status": "available", "initial_owner": "", "status": "available", "owner": ""})
        write_json(self.root / "state" / "chapters" / "chapter-0050.json", {"chapter": 50, "title": "旧约定", "summary": "长期线索的旧约定", "entities": ["ITEM-0001"]})
        events = "\n".join(json.dumps({"id": f"EVT-{i:04d}-001", "chapter": i, "sequence": 1, "type": "note", "summary": "长期线索 既有约定", "state_effect": False}, ensure_ascii=False) for i in range(1, 5)) + "\n"
        (self.root / "state" / "events" / "events.jsonl").write_text(events, encoding="utf-8")
        chosen = balanced_select(collect(self.root, "长期线索 既有约定", strict_events=True), 8)
        kinds = {item.kind for item in chosen}
        self.assertTrue({"canon", "arc", "event", "chapter", "entity"}.issubset(kinds), kinds)


    def test_symlinked_managed_paths_are_rejected_before_write(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        init_project(self.root)
        outside_file = Path(self.temp.name) / "outside-events.jsonl"
        outside_file.write_text("KEEP\n", encoding="utf-8")
        events = self.root / "state" / "events" / "events.jsonl"
        events.unlink()
        try:
            os.symlink(outside_file, events)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        draft(self.root, 1, "起点")
        d1 = delta_for(1, "起点", "不应写出")
        d1["events"] = [{"type": "note", "summary": "不应写出", "state_effect": False}]
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(outside_file.read_text(encoding="utf-8"), "KEEP\n")

        # A linked report directory must also be rejected before creating an outside report.
        events.unlink()
        events.write_text("", encoding="utf-8")
        outside_dir = Path(self.temp.name) / "outside-audits"
        outside_dir.mkdir()
        shutil.rmtree(self.root / "audits")
        os.symlink(outside_dir, self.root / "audits", target_is_directory=True)
        scan = run_script("quality_scan.py", str(self.root))
        self.assertNotEqual(scan.returncode, 0)
        self.assertEqual(list(outside_dir.iterdir()), [])


    def test_linked_revision_directory_is_rejected(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        init_project(self.root)
        draft(self.root, 1, "起点", "旧正文。")
        commit(self.root, 1, delta_for(1, "起点", "旧版本"))
        outside = Path(self.temp.name) / "outside-revisions"
        outside.mkdir()
        shutil.rmtree(self.root / "revisions")
        try:
            os.symlink(outside, self.root / "revisions", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        draft(self.root, 1, "起点", "新正文。")
        proc = run_script("rewrite_prose.py", str(self.root), "--chapter", "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_hard_linked_event_log_is_rejected(self) -> None:
        if not hasattr(os, "link"):
            self.skipTest("hard links unavailable")
        init_project(self.root)
        events = self.root / "state/events/events.jsonl"
        outside = Path(self.temp.name) / "outside-hardlink.jsonl"
        try:
            os.link(events, outside)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        draft(self.root, 1, "起点")
        proc = commit(self.root, 1, delta_for(1, "起点", "不应提交"), expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(outside.read_text(encoding="utf-8"), "")

    def test_created_chapter_and_initial_fields_are_immutable(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "角色甲", "created_chapter": 0})
        d1 = delta_for(1, "出现", "实体出现")
        d1["entity_changes"] = [{"id": "CHAR-0001", "create": True, "patch": character}]
        draft(self.root, 1, "出现")
        commit(self.root, 1, d1)
        entity = json.loads((self.root / "state/entities/characters/CHAR-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(entity["created_chapter"], 1)
        self.assertTrue((self.root / "state/baselines/CHAR-0001.json").is_file())
        activate_final_tail(self.root)

        d2 = delta_for(2, "篡改", "试图改写历史")
        d2["entity_changes"] = [{"id": "CHAR-0001", "patch": {"initial_knowledge": ["FACT-0001"]}}]
        draft(self.root, 2, "篡改")
        proc = commit(self.root, 2, d2, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("immutable history", proc.stderr)

        entity["initial_knowledge"] = ["FACT-0001"]
        write_json(self.root / "state/entities/characters/CHAR-0001.json", entity)
        audit = run_script("audit_project.py", str(self.root))
        self.assertEqual(audit.returncode, 1)
        self.assertIn("不可变", (self.root / "audits/latest-audit.md").read_text(encoding="utf-8"))

    def test_knowledge_used_and_event_schema_are_strict(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "角色甲"})
        d1 = delta_for(1, "错误", "错误类型")
        d1["entity_changes"] = [{"id": "CHAR-0001", "create": True, "patch": character}]
        d1["knowledge_used"] = {"CHAR-0001": "FACT-0001"}
        draft(self.root, 1, "错误")
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must be a list", proc.stderr)

        d1["knowledge_used"] = {}
        d1["events"] = [{"type": "character_status_changed", "status": "dead"}]
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("character_id", proc.stderr)

        d1["events"] = [{"type": "note", "summary": "坏序号", "state_effect": False, "sequence": "oops"}]
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("sequence", proc.stderr)

        d1["events"] = [{"type": "note", "summary": "布尔值不是序号", "state_effect": False, "chapter": True, "sequence": True}]
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("sequence", proc.stderr)

        item = json.loads((ASSETS / "item-template.json").read_text(encoding="utf-8"))
        item.update({"id": "ITEM-0001", "name": "错误主体"})
        d1["entity_changes"].append({"id": "ITEM-0001", "create": True, "patch": item})
        d1["events"] = [{"type": "character_status_changed", "character_id": "ITEM-0001", "status": "dead"}]
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CHAR-", proc.stderr)

    def test_state_event_requires_patch_and_custom_sequence_sets_event_id(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "角色甲"})
        d1 = delta_for(1, "建立角色", "角色登场")
        d1["entity_changes"] = [{"id": "CHAR-0001", "create": True, "patch": character}]
        draft(self.root, 1, "建立角色")
        commit(self.root, 1, d1)
        activate_final_tail(self.root)

        d2 = delta_for(2, "状态变化", "角色状态变化")
        d2["events"] = [{"type": "character_status_changed", "character_id": "CHAR-0001", "status": "dead", "sequence": 5}]
        draft(self.root, 2, "状态变化")
        proc = commit(self.root, 2, d2, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("matching entity_changes patch", proc.stderr)

        character["created_chapter"] = 1
        character["status"] = "dead"
        d2["entity_changes"] = [{"id": "CHAR-0001", "patch": {"status": "dead"}}]
        commit(self.root, 2, d2)
        meta = json.loads((self.root / "state/chapters/chapter-0002.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["events"], ["EVT-0002-005"])

    def test_relationship_event_optional_fields_are_typed(self) -> None:
        init_project(self.root)
        rel = json.loads((ASSETS / "relationship-template.json").read_text(encoding="utf-8"))
        rel.update({"id": "REL-0001", "name": "关系"})
        d1 = delta_for(1, "关系", "关系变化")
        d1["entity_changes"] = [{"id": "REL-0001", "create": True, "patch": rel}]
        d1["events"] = [{"type": "relationship_changed", "relationship_id": "REL-0001", "stage": 123}]
        draft(self.root, 1, "关系")
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("stage must be a non-empty string", proc.stderr)

    def test_corrupt_event_is_reported_without_audit_traceback(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "起点")
        commit(self.root, 1, delta_for(1, "起点", "开始"))
        with (self.root / "state/events/events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "EVT-0002-001", "chapter": 2, "sequence": "oops", "type": "note", "summary": "坏记录", "state_effect": False}, ensure_ascii=False) + "\n")
        audit = run_script("audit_project.py", str(self.root))
        self.assertEqual(audit.returncode, 1)
        self.assertNotIn("Traceback", audit.stderr)
        self.assertIn("sequence", (self.root / "audits/latest-audit.md").read_text(encoding="utf-8"))

    def test_committed_transaction_without_backup_is_cleanup_only(self) -> None:
        init_project(self.root)
        tx = "commit-0001-cccccccccc"
        write_json(self.root / ".novel/transaction.json", {
            "schema": 1,
            "transaction_id": tx,
            "status": "committed",
            "chapter": 1,
            "backup_dir": f".novel/backups/{tx}",
            "events_size": 0,
            "files": [{"path": "state/current.json", "existed": True}],
        })
        proc = run_script("recover_project.py", str(self.root), check=True)
        self.assertIn("finalized committed cleanup", proc.stdout)
        self.assertFalse((self.root / ".novel/transaction.json").exists())

    def test_recovery_never_expands_event_log(self) -> None:
        init_project(self.root)
        tx = "commit-0001-dddddddddd"
        backup = self.root / ".novel/backups" / tx / "state"
        backup.mkdir(parents=True)
        shutil.copy2(self.root / "state/current.json", backup / "current.json")
        write_json(self.root / ".novel/transaction.json", {
            "schema": 1,
            "transaction_id": tx,
            "status": "dirty",
            "chapter": 1,
            "backup_dir": f".novel/backups/{tx}",
            "events_size": 100_000_000,
            "files": [{"path": "state/current.json", "existed": True}],
        })
        proc = run_script("recover_project.py", str(self.root))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual((self.root / "state/events/events.jsonl").stat().st_size, 0)

    def test_future_event_cannot_satisfy_quest_prerequisite(self) -> None:
        init_project(self.root)
        quest = json.loads((ASSETS / "quest-template.json").read_text(encoding="utf-8"))
        quest.update({
            "id": "QUEST-0001",
            "name": "不应开启",
            "initial_status": "active",
            "status": "active",
            "prerequisites": [{"type": "event_exists", "id": "EVT-0002-001"}],
        })
        d1 = delta_for(1, "任务", "错误开启任务")
        d1["entity_changes"] = [{"id": "QUEST-0001", "create": True, "patch": quest}]
        draft(self.root, 1, "任务")
        proc = commit(self.root, 1, d1, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("prerequisite event", proc.stderr)

    def test_same_chapter_knowledge_sequence_is_historical(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "角色甲", "knowledge": ["FACT-0001"]})
        d1 = delta_for(1, "先说后知", "先使用秘密，后获得")
        d1["entity_changes"] = [{"id": "CHAR-0001", "create": True, "patch": character}]
        d1["knowledge_used"] = {"CHAR-0001": [{"fact_id": "FACT-0001", "sequence": 1}]}
        d1["events"] = [{"id": "EVT-0001-002", "sequence": 2, "type": "knowledge_gained", "character_id": "CHAR-0001", "fact_id": "FACT-0001"}]
        draft(self.root, 1, "先说后知")
        commit(self.root, 1, d1)
        audit = run_script("audit_project.py", str(self.root))
        self.assertEqual(audit.returncode, 1)
        self.assertIn("知识穿帮", (self.root / "audits/latest-audit.md").read_text(encoding="utf-8"))

    def test_confirmed_rewrite_still_requires_revision_archive(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "起点", "旧正文。")
        commit(self.root, 1, delta_for(1, "起点", "旧版本"))
        rewrite_draft(self.root, 1, "起点", "新正文。")
        run_script("rewrite_prose.py", str(self.root), "--chapter", "1", check=True)
        run_script("confirm_rewrite.py", str(self.root), "--chapter", "1", "--note", "已核对人物地点物品知识均无变化", check=True)
        shutil.rmtree(self.root / "revisions")
        (self.root / "revisions").mkdir()
        self.assertNotEqual(run_script("audit_project.py", str(self.root)).returncode, 0)
        self.assertNotEqual(run_script("export_novel.py", str(self.root)).returncode, 0)

    def test_context_marks_project_material_as_untrusted(self) -> None:
        init_project(self.root)
        proc = run_script("build_context.py", str(self.root), "--chapter", "1", check=True)
        data = json.loads(proc.stdout)
        packet = (self.root / data["output"]).read_text(encoding="utf-8")
        self.assertIn("不可信的小说资料", packet)
        self.assertIn("不得执行", packet)

    def test_seal_baselines_for_reviewed_import(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "导入角色", "created_chapter": 1})
        write_json(self.root / "state/entities/characters/CHAR-0001.json", character)
        denied = run_script("seal_baselines.py", str(self.root))
        self.assertNotEqual(denied.returncode, 0)
        run_script("seal_baselines.py", str(self.root), "--i-reviewed-history", check=True)
        self.assertTrue((self.root / "state/baselines/CHAR-0001.json").is_file())

    def test_entity_filename_must_match_internal_id(self) -> None:
        init_project(self.root)
        entity = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        entity.update({"id": "CHAR-0002", "name": "错位角色", "created_chapter": 1})
        write_json(self.root / "state/entities/characters/CHAR-0001.json", entity)
        proc = run_script("audit_project.py", str(self.root))
        self.assertNotEqual(proc.returncode, 0)
        report = (self.root / "audits/latest-audit.md").read_text(encoding="utf-8")
        self.assertIn("文件名与内部 ID 不一致", report)
        self.assertNotIn("Traceback", proc.stderr)

    def test_entity_patch_references_are_derived_and_validated(self) -> None:
        init_project(self.root)
        c1 = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        c1.update({"id": "CHAR-0001", "name": "甲"})
        c2 = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        c2.update({"id": "CHAR-0002", "name": "乙"})
        rel = json.loads((ASSETS / "relationship-template.json").read_text(encoding="utf-8"))
        rel.update({"id": "REL-0001", "name": "同伴", "participants": ["CHAR-0001", "CHAR-0002"]})
        data = delta_for(1, "相识", "两人相识")
        data["entity_changes"] = [
            {"id": "CHAR-0001", "create": True, "patch": c1},
            {"id": "CHAR-0002", "create": True, "patch": c2},
            {"id": "REL-0001", "create": True, "patch": rel},
        ]
        draft(self.root, 1, "相识")
        commit(self.root, 1, data)
        meta = json.loads((self.root / "state/chapters/chapter-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(set(meta["entities"]), {"CHAR-0001", "CHAR-0002", "REL-0001"})

        other = Path(self.temp.name) / "missing-ref-book"
        init_project(other)
        bad_rel = dict(rel)
        bad_rel["participants"] = ["CHAR-9999"]
        bad = delta_for(1, "错误关系", "引用不存在角色")
        bad["entity_changes"] = [{"id": "REL-0001", "create": True, "patch": bad_rel}]
        draft(other, 1, "错误关系")
        proc = commit(other, 1, bad, expect_ok=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("missing entity", proc.stderr)

    def test_malformed_chapter_metadata_is_reported_without_traceback(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "起点")
        commit(self.root, 1, delta_for(1, "起点", "故事开始"))
        meta_path = self.root / "state/chapters/chapter-0001.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["chapter"] = "abc"
        write_json(meta_path, meta)
        proc = run_script("audit_project.py", str(self.root))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        report = (self.root / "audits/latest-audit.md").read_text(encoding="utf-8")
        self.assertIn("元数据 chapter must be an integer", report)

    def test_memory_search_rejects_malformed_chapter_without_traceback(self) -> None:
        init_project(self.root)
        write_json(self.root / "state/chapters/chapter-0001.json", {
            "schema": 1, "chapter": "abc", "title": "坏数据", "summary": "长期线索",
            "entities": [], "events": [], "depends_on_events": [],
        })
        proc = run_script("search_memory.py", str(self.root), "长期线索")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("must be an integer", proc.stderr)

    def test_malformed_entity_schema_is_reported_without_traceback(self) -> None:
        init_project(self.root)
        write_json(self.root / "state/entities/characters/CHAR-0001.json", {
            "schema": "oops", "id": "CHAR-0001", "name": "坏数据", "status": "active",
            "created_chapter": "oops", "aliases": [], "initial_status": "active",
            "initial_location": "", "initial_knowledge": [], "initial_skills": [],
            "knowledge": [], "skills": []
        })
        proc = run_script("audit_project.py", str(self.root))
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stderr)
        report = (self.root / "audits/latest-audit.md").read_text(encoding="utf-8")
        self.assertIn("entity schema must be integer 1", report)
        self.assertIn("created_chapter must be an integer", report)

    def test_relevant_arc_requires_exact_heading(self) -> None:
        init_project(self.root)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        current["relevant_arcs"] = ["ARC-001"]
        write_json(self.root / "state/current.json", current)
        (self.root / "state/arc-summaries/ARC-001.md").write_text("# ARC-010 错误故事弧\n", encoding="utf-8")
        context = run_script("build_context.py", str(self.root), "--chapter", "1")
        self.assertEqual(context.returncode, 0, context.stderr)
        payload = json.loads(context.stdout)
        self.assertTrue(payload["ready"])
        packet = (self.root / payload["output"]).read_text(encoding="utf-8")
        self.assertNotIn("错误故事弧", packet)
        audit = run_script("audit_project.py", str(self.root))
        self.assertNotEqual(audit.returncode, 0)

    def test_event_append_handles_partial_os_writes(self) -> None:
        init_project(self.root)
        event_path = self.root / "state/events/events.jsonl"
        real_write = common.os.write

        def partial_write(fd: int, data: object) -> int:
            payload = bytes(data)
            return real_write(fd, payload[: max(1, len(payload) // 4)])

        rows = [{"id": "EVT-0001-001", "chapter": 1, "sequence": 1, "type": "note", "summary": "很长的可靠写入测试" * 80, "state_effect": False}]
        with mock.patch.object(common.os, "write", side_effect=partial_write):
            append_jsonl_no_follow(event_path, rows, self.root)
        loaded = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(loaded, rows)

    def test_single_wrapper_dispatches_trusted_commands(self) -> None:
        target = Path(self.temp.name) / "wrapper-book"
        proc = run_script("novelctl.py", "init", str(target), "--title", "入口验证", "--genre", "通用类型")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        context = run_script("novelctl.py", "context", str(target), "--chapter", "1")
        self.assertEqual(context.returncode, 0, context.stderr)
        self.assertTrue(json.loads(context.stdout)["ready"])
        bad = run_script("novelctl.py", "not-a-command")
        self.assertEqual(bad.returncode, 2)

    def test_novel_creator_skill_and_agent_task_cards(self) -> None:
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: novel-creator-flash", skill_text)
        self.assertIn("每个写手一次负责连续 5 章", skill_text)
        self.assertIn("启用 10 个写手", skill_text)
        self.assertIn("规划 50 章", skill_text)
        self.assertIn("prepare-production", skill_text)
        self.assertIn("production-status", skill_text)
        self.assertIn('Bash("${CLAUDE_SKILL_DIR}/scripts/novelctl-skill" *)', skill_text)
        self.assertNotIn('Bash("${CLAUDE_SKILL_DIR}/scripts/novelctl" *)', skill_text)
        self.assertIn("并行执行席位", skill_text)
        self.assertIn("novel-fast-continuity-reviewer", skill_text)
        self.assertIn("--continuity-risk low|high", skill_text)
        self.assertIn("CLAUDE.md", skill_text)
        self.assertNotIn("Humanizer Agent", skill_text)

        agents_root = ROOT / ".claude" / "agents"
        expected = {*(f"novel-fast-writer-{i}.md" for i in range(1, 11)),
                    "novel-fast-reader-flow.md", "novel-fast-reader-character.md", "novel-fast-reader-hook.md",
                    "novel-fast-continuity-reviewer.md"}
        self.assertEqual({path.name for path in agents_root.glob("*.md")}, expected)
        for index in range(1, 11):
            writer = (agents_root / f"novel-fast-writer-{index}.md").read_text(encoding="utf-8")
            self.assertIn("background: true", writer)
            self.assertIn("连续五章原料块", writer)
            self.assertIn("按章号顺序写完", writer)
            self.assertIn("possible_continuity_risks", writer)
            self.assertIn("report JSON", writer)
        for name in ("flow", "character", "hook"):
            reader = (agents_root / f"novel-fast-reader-{name}.md").read_text(encoding="utf-8")
            self.assertNotIn("TodoWrite", reader)
            self.assertIn("Read(.novel/blind-packets/**)", reader)
            self.assertIn("background: true", reader)
            reader_tools = reader.split("tools:", 1)[1].split("disallowedTools:", 1)[0]
            self.assertIn("Read(.novel/blind-packets/**)", reader_tools)
        self.assertIn("综合盲读主席", (agents_root / "novel-fast-reader-flow.md").read_text(encoding="utf-8"))

    def test_parallel_production_manifest_and_status(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        configured = run_script(
            "configure_project.py", str(self.root),
            "--writer-pool-size", "10", "--blind-reader-count", "1",
            "--min-chars", "10", "--target-chars", "12", "--soft-max-chars", "30", check=True,
        )
        self.assertEqual(json.loads(configured.stdout)["production"]["writer_pool_size"], 10)
        prepared = run_script("prepare_production.py", str(self.root), "--writers", "10", "--chapters", "50", check=True)
        payload = json.loads(prepared.stdout)
        self.assertEqual(payload["planned_chapters"], 50)
        self.assertEqual(payload["writer_count"], 10)
        self.assertEqual(len(payload["assignments"]), 10)
        self.assertEqual(payload["assignments"][0]["start_chapter"], 1)
        self.assertEqual(payload["assignments"][-1]["end_chapter"], 50)
        for item in payload["assignments"]:
            self.assertEqual(len(item["outputs"]), 5)
            for chapter, rel in zip(range(item["start_chapter"], item["end_chapter"] + 1), item["outputs"]):
                target = self.root / rel; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"# 第{chapter}章 原料\n\n有效正文内容甲乙丙丁戊己庚辛壬癸。\n", encoding="utf-8")
            report = self.root / item["report"]
            write_json(report, {
                "schema": 1, "writer": item["writer"], "start_chapter": item["start_chapter"], "end_chapter": item["end_chapter"],
                "newly_invented_details": [], "character_micro_changes": [], "new_promises": [], "motifs_or_objects": [],
                "strong_lines_or_moments": [], "possible_continuity_risks": [],
            })
        status = json.loads(run_script("production_status.py", str(self.root), check=True).stdout)
        self.assertTrue(status["all_ready"])
        self.assertEqual(len(status["blocks"]), 10)
        self.assertFalse(status["continuity_risk_detected"])

    def test_persisted_writer_risk_forces_high_continuity_review(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        run_script(
            "configure_project.py", str(self.root),
            "--writer-pool-size", "1", "--blind-reader-count", "1",
            "--min-chars", "1", "--target-chars", "1", "--soft-max-chars", "1000", check=True,
        )
        prepared = json.loads(run_script(
            "prepare_production.py", str(self.root), "--writers", "1", "--chapters", "5", check=True
        ).stdout)
        assignment = prepared["assignments"][0]
        for number, rel in zip(range(1, 6), assignment["outputs"]):
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# 第{number}章 原料\n\n正文内容。\n", encoding="utf-8")
            draft(self.root, number, f"批次{number}", "正文内容。")
        write_json(self.root / assignment["report"], {
            "schema": 1, "writer": assignment["writer"],
            "start_chapter": 1, "end_chapter": 5,
            "newly_invented_details": [], "character_micro_changes": [],
            "new_promises": [], "motifs_or_objects": [], "strong_lines_or_moments": [],
            "possible_continuity_risks": ["第三章道具归属可能与第一章冲突"],
        })
        blocked = run_script("prepare_batch_review.py", str(self.root), "--continuity-risk", "low")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("must be high", blocked.stderr)
        high = run_script("prepare_batch_review.py", str(self.root), "--continuity-risk", "high", check=True)
        payload = json.loads(high.stdout)
        self.assertTrue(payload["writer_report_risks"])
        record = json.loads((self.root / payload["output"]).read_text(encoding="utf-8"))
        self.assertEqual(record["continuity"]["writer_report_risk_count"], 1)
        self.assertTrue(any("道具归属" in item for item in record["continuity"]["risk_reasons"]))

    def test_reader_panel_requires_configured_count(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        run_script(
            "configure_project.py", str(self.root),
            "--blind-reader-count", "3", "--min-chars", "1", "--target-chars", "1", "--soft-max-chars", "1000",
            check=True,
        )
        for number in range(1, 6):
            draft(self.root, number, f"批次{number}", "正文内容。")
        prepared = run_script("prepare_batch_review.py", str(self.root), "--continuity-risk", "low", check=True)
        record_path = self.root / json.loads(prepared.stdout)["output"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["first_reader"].update({
            "status": "completed",
            "completed_readers": ["novel-fast-reader-flow", "novel-fast-reader-character"],
            "verdict": "acceptable",
            "ending_pull": "fair",
            "revision_applied": True,
            "issue_tags": [],
            "highest_value_revision": "",
        })
        record["continuity"].update({
            "status": "completed", "checked_by": "main-agent",
            "blocking_count": 0, "warning_count": 0,
        })
        write_json(record_path, record)
        blocked = run_script("finalize_batch_review.py", str(self.root))
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("fewer completed readers", blocked.stderr)
        record["first_reader"]["completed_readers"].append("novel-fast-reader-hook")
        write_json(record_path, record)
        finalized = run_script("finalize_batch_review.py", str(self.root))
        self.assertEqual(finalized.returncode, 0, finalized.stderr)

    def test_two_reader_panel_defaults_to_flow_and_hook(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        run_script(
            "configure_project.py", str(self.root),
            "--blind-reader-count", "2", "--min-chars", "1", "--target-chars", "1", "--soft-max-chars", "1000",
            check=True,
        )
        prepared = json.loads(run_script("prepare_production.py", str(self.root), "--writers", "1", "--chapters", "5", check=True).stdout)
        self.assertEqual([job["reader"] for job in prepared["reader_jobs"]], ["novel-fast-reader-flow", "novel-fast-reader-hook"])
        for number in range(1, 6):
            draft(self.root, number, f"批次{number}", "正文内容。")
        review = json.loads(run_script("prepare_batch_review.py", str(self.root), "--continuity-risk", "low", check=True).stdout)
        record = json.loads((self.root / review["output"]).read_text(encoding="utf-8"))
        self.assertEqual(record["first_reader"]["available_readers"], ["novel-fast-reader-flow", "novel-fast-reader-hook"])

    def test_high_risk_batch_requires_fast_continuity_reviewer(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        run_script(
            "configure_project.py", str(self.root),
            "--blind-reader-count", "2", "--min-chars", "1", "--target-chars", "1", "--soft-max-chars", "1000",
            check=True,
        )
        for number in range(1, 6):
            draft(self.root, number, f"批次{number}", "正文内容。")
        prepared = json.loads(run_script(
            "prepare_batch_review.py", str(self.root),
            "--continuity-risk", "high",
            "--risk-reason", "writer-3 reported item ownership drift",
            check=True,
        ).stdout)
        record_path = self.root / prepared["output"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["continuity"]["risk_level"], "high")
        record["first_reader"].update({
            "status": "completed",
            "completed_readers": ["novel-fast-reader-flow", "novel-fast-reader-hook"],
            "verdict": "acceptable",
            "ending_pull": "fair",
            "revision_applied": True,
            "issue_tags": [],
            "highest_value_revision": "",
        })
        record["continuity"].update({
            "status": "completed", "checked_by": "main-agent",
            "blocking_count": 0, "warning_count": 0,
        })
        write_json(record_path, record)
        blocked = run_script("finalize_batch_review.py", str(self.root))
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("novel-fast-continuity-reviewer", blocked.stderr)
        record["continuity"]["checked_by"] = "novel-fast-continuity-reviewer"
        write_json(record_path, record)
        finalized = run_script("finalize_batch_review.py", str(self.root))
        self.assertEqual(finalized.returncode, 0, finalized.stderr)

    def test_raw_writer_output_cannot_be_committed_as_canonical(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        run_script(
            "configure_project.py", str(self.root),
            "--min-chars", "1", "--target-chars", "1", "--soft-max-chars", "1000",
            check=True,
        )
        prepared = json.loads(run_script("prepare_production.py", str(self.root), check=True).stdout)
        first = prepared["assignments"][0]
        raw = self.root / first["outputs"][0]
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("# 第1章 原料\n\n只存在于写手原料目录。\n", encoding="utf-8")
        write_json(self.root / "state/deltas/chapter-0001.json", delta_for(1, "原料", "测试"))
        proc = run_script("commit_chapter.py", str(self.root), "--chapter", "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("staging", proc.stderr.lower())
        self.assertFalse((self.root / "chapters/chapter-0001.md").exists())

    def test_default_length_settings_and_stats_metric(self) -> None:
        self.root.mkdir(parents=True)
        run_script("init_project.py", str(self.root), "--title", "篇幅测试", "--genre", "通用", check=True)
        settings = json.loads((self.root / "state/writing-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["chapter_length"]["minimum_effective_chars"], 2700)
        self.assertEqual(settings["chapter_length"]["target_effective_chars"], 3200)
        self.assertEqual(settings["batch"], {"batch_size": 5, "planning_window": 10})
        self.assertEqual(settings["production"], {"writer_pool_size": 5, "blind_reader_count": 1})
        self.assertNotIn("maximum_" + "expansion_attempts", settings["chapter_length"])
        (self.root / ".novel/staging/chapter-0001.md").write_text(
            "# 第1章 测试\n\n甲，乙！abc 12。\n", encoding="utf-8"
        )
        proc = run_script("novelctl.py", "chapter-stats", str(self.root), "--chapter", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["effective_chars"], 7)
        self.assertEqual(result["status"], "too_short")
        self.assertEqual(result["shortfall"], 2693)
        self.assertEqual(result["metric"], "letters_and_digits_excluding_heading_whitespace_and_punctuation")

    def test_invalid_length_settings_are_reported_without_traceback(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "设置", "正文内容")
        (self.root / "state/writing-settings.json").write_text(
            '{"schema": 1, "chapter_length": {"minimum_effective_chars": "many"}}',
            encoding="utf-8",
        )
        stats = run_script("novelctl.py", "chapter-stats", str(self.root), "--chapter", "1")
        self.assertEqual(stats.returncode, 2)
        self.assertIn("minimum_effective_chars must be an integer", stats.stderr)
        self.assertNotIn("Traceback", stats.stderr)
        audit = run_script("novelctl.py", "audit", str(self.root))
        self.assertNotEqual(audit.returncode, 0)
        report = (self.root / "audits/latest-audit.md").read_text(encoding="utf-8")
        self.assertIn("章节篇幅设置无效", report)

    def test_short_chapter_is_blocked_and_override_is_recorded(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=1)
        draft(self.root, 1, "起点", "正文内容")
        data = delta_for(1, "起点", "故事开始")
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        batch = current.get("final_tail") or current["batch"]
        data["current_patch"]["reader_review"] = {
            "reviewed_through_chapter": 1, "reason": "batch", "verdict": "acceptable",
            "ending_pull": "fair", "revision_applied": True, "issue_tags": [],
            "highest_value_revision": "", "batch_id": batch["batch_id"],
            "batch_start_chapter": batch["start_chapter"], "batch_end_chapter": batch["end_chapter"],
            "review_kind": batch.get("review_kind", "batch"),
        }
        write_json(self.root / "state/deltas/chapter-0001.json", data)
        blocked = run_script("novelctl.py", "commit", str(self.root), "--chapter", "1")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("chapter is too short", blocked.stderr)
        self.assertIn("shortfall=2696", blocked.stderr)
        self.assertTrue((self.root / ".novel/staging/chapter-0001.md").is_file())
        self.assertFalse((self.root / "chapters/chapter-0001.md").exists())

        finalize_batch_review_from_delta(self.root, 1, data)
        accepted = run_script(
            "novelctl.py", "commit", str(self.root), "--chapter", "1",
            "--min-chars", "4", "--target-chars", "4", "--soft-max-chars", "100",
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        meta = json.loads((self.root / "state/chapters/chapter-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["length"]["effective_chars"], 4)
        self.assertEqual(meta["length"]["minimum_effective_chars"], 4)
        self.assertEqual(meta["length"]["status"], "on_target")

    def test_init_creates_lightweight_style_lessons_and_session_handoff(self) -> None:
        init_project(self.root)
        self.assertFalse((self.root / "drafts/agent-work").exists())
        self.assertFalse((self.root / "plot/chapter-plans").exists())
        self.assertTrue((self.root / ".novel/staging").is_dir())
        contract = self.root / "canon/prose-contract.md"
        self.assertTrue(contract.is_file())
        contract_text = contract.read_text(encoding="utf-8")
        self.assertIn("narrative distance", contract_text)
        self.assertIn("机械感风险", contract_text)
        lessons = self.root / "state/creative-lessons.md"
        self.assertTrue(lessons.is_file())
        self.assertIn("当前有效经验", lessons.read_text(encoding="utf-8"))
        handoff = self.root / "state/session-handoff.md"
        self.assertTrue(handoff.is_file())
        self.assertIn("会话交接", handoff.read_text(encoding="utf-8"))

    def test_context_includes_session_handoff_as_optional_memory(self) -> None:
        init_project(self.root)
        marker = "HANDOFF-MARKER-保持主角暂不揭露秘密"
        (self.root / "state/session-handoff.md").write_text(
            "# 会话交接\n\n## 已确认的创作决定\n\n- " + marker + "\n",
            encoding="utf-8",
        )
        proc = run_script("build_context.py", str(self.root), "--chapter", "1", check=True)
        output = self.root / json.loads(proc.stdout)["output"]
        text = output.read_text(encoding="utf-8")
        self.assertIn(marker, text)
        self.assertIn("会话交接与有效经验", text)

    def test_shell_installer_installs_fast_production_agents(self) -> None:
        project = Path(self.temp.name) / "install-project"
        project.mkdir()
        installer = ROOT / "install.sh"
        ps_installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("[Parameter(Position=0)]", ps_installer)
        self.assertTrue((ROOT / "tests/smoke_claude_skill_permissions.py").is_file())
        first = subprocess.run(
            ["bash", str(installer), str(project)],
            text=True, capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        skill_target = project / ".claude/skills/novel-creator-flash"
        agents_target = project / ".claude/agents"
        self.assertTrue((skill_target / "SKILL.md").is_file())
        agent_names = [*(f"novel-fast-writer-{i}.md" for i in range(1, 11)),
                       "novel-fast-reader-flow.md", "novel-fast-reader-character.md", "novel-fast-reader-hook.md",
                       "novel-fast-continuity-reviewer.md"]
        for name in agent_names:
            self.assertTrue((agents_target / name).is_file())
        (skill_target / "old-marker.txt").write_text("old skill", encoding="utf-8")
        (agents_target / "novel-fast-writer-1.md").write_text("old writer", encoding="utf-8")
        second = subprocess.run(
            ["bash", str(installer), "--scope", "project", "--project-path", str(project), "--force"],
            text=True, capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        backups = sorted((project / ".claude/backups").glob("novel-creator-flash-*"))
        self.assertTrue(backups)
        latest = backups[-1]
        self.assertEqual((latest / "skill/old-marker.txt").read_text(encoding="utf-8"), "old skill")
        self.assertEqual((latest / "agents/novel-fast-writer-1.md").read_text(encoding="utf-8"), "old writer")
        self.assertIn("name: novel-fast-writer-1", (agents_target / "novel-fast-writer-1.md").read_text(encoding="utf-8"))

    def test_writer_staging_is_atomically_promoted_and_preserved_on_failure(self) -> None:
        init_project(self.root)
        staging = self.root / ".novel/staging/chapter-0001.md"
        formal = self.root / "chapters/chapter-0001.md"
        staging.write_text(
            "# 第1章 起点\n\n人物从正在发生的场景开始行动。\n", encoding="utf-8"
        )
        broken = delta_for(1, "", "故事开始")
        write_json(self.root / "state/deltas/chapter-0001.json", broken)
        failed = run_script("novelctl.py", "commit", str(self.root), "--chapter", "1")
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(staging.is_file())
        self.assertFalse(formal.exists())

        proc = commit(self.root, 1, delta_for(1, "起点", "故事开始"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(staging.exists())
        self.assertTrue(formal.is_file())
        self.assertIn("人物从正在发生的场景开始行动", formal.read_text(encoding="utf-8"))
        self.assertTrue((self.root / "state/chapters/chapter-0001.json").is_file())

        write_json(self.root / "state/deltas/chapter-0002.json", delta_for(2, "继续", "继续推进"))
        missing = run_script("novelctl.py", "commit", str(self.root), "--chapter", "2")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn(".novel/staging/chapter-0002.md", missing.stderr.replace("\\", "/"))
        self.assertNotIn("Traceback", missing.stderr)

    def test_hard_linked_staging_is_rejected(self) -> None:
        if not hasattr(os, "link"):
            self.skipTest("hard links unavailable")
        init_project(self.root)
        outside = Path(self.temp.name) / "outside-prose.md"
        outside.write_text("# 第1章 起点\n\n外部正文。\n", encoding="utf-8")
        staging = self.root / ".novel/staging/chapter-0001.md"
        try:
            os.link(outside, staging)
        except OSError as exc:
            self.skipTest(str(exc))
        write_json(self.root / "state/deltas/chapter-0001.json", delta_for(1, "起点", "测试"))
        proc = run_script("novelctl.py", "commit", str(self.root), "--chapter", "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("hard-linked chapter input", proc.stderr)
        self.assertFalse((self.root / "chapters/chapter-0001.md").exists())
        self.assertTrue(outside.is_file())

    def test_uncommitted_formal_chapter_is_not_used_as_default_input(self) -> None:
        init_project(self.root)
        formal = self.root / "chapters/chapter-0001.md"
        formal.write_text("# 第1章 错放\n\n不应被默认提交。\n", encoding="utf-8")
        write_json(self.root / "state/deltas/chapter-0001.json", delta_for(1, "错放", "测试"))
        proc = run_script("novelctl.py", "commit", str(self.root), "--chapter", "1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(".novel/staging/chapter-0001.md", proc.stderr.replace("\\", "/"))
        self.assertFalse((self.root / "state/chapters/chapter-0001.json").exists())

    def test_entity_candidates_are_advisory_and_do_not_modify_state(self) -> None:
        init_project(self.root)
        character = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        character.update({"id": "CHAR-0001", "name": "角色甲", "created_chapter": 1})
        write_json(self.root / "state/entities/characters/CHAR-0001.json", character)
        staging = self.root / ".novel/staging/chapter-0001.md"
        staging.write_text(
            "# 第1章 发现\n\n角色甲在黑石塔前停下，低声说：【银纹钥匙】已经不见了。"
            "他把手搭上门闩，却没有推门，随后松开门闩。\n",
            encoding="utf-8",
        )
        before = (self.root / "state/current.json").read_bytes()
        proc = run_script("novelctl.py", "entity-candidates", str(self.root), "--chapter", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["advisory"])
        self.assertFalse(data["blocking"])
        self.assertTrue(any(item["id"] == "CHAR-0001" for item in data["possibly_missing_from_delta"]))
        terms = {item["term"] for item in data["possible_new_terms"]}
        self.assertIn("黑石塔", terms)
        self.assertFalse(any("门闩" in term or "把手搭上门" in term or "松开门" in term for term in terms))
        self.assertEqual((self.root / "state/current.json").read_bytes(), before)
        self.assertFalse((self.root / "state/entities/locations/LOC-0001.json").exists())

    def test_prepare_delta_scaffolds_title_progress_questions_and_scene_bridge(self) -> None:
        init_project(self.root, test_batch_size=5)
        (self.root / ".novel/staging/chapter-0001.md").write_text(
            "# 第一章 起点\n\n人物在门前停下。\n", encoding="utf-8"
        )
        proc = run_script("novelctl.py", "prepare-delta", str(self.root), "--chapter", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["prepared"])
        self.assertEqual(result["title"], "起点")
        self.assertEqual(len(result["review_before_commit"]), 3)
        self.assertIn("推进、深化或蓄势", result["review_before_commit"][0])
        self.assertIn("下一章为什么不能", result["review_before_commit"][2])
        delta_path = self.root / result["output"]
        data = json.loads(delta_path.read_text(encoding="utf-8"))
        self.assertEqual(data["outline_node"], "NODE-001")
        self.assertEqual(set(data["current_patch"]), {
            "current_location", "point_of_view", "scene_entities", "current_goal", "scene_bridge"
        })
        self.assertEqual(set(data["current_patch"]["scene_bridge"]), {
            "time", "location", "pov", "last_action", "immediate_pressure", "emotional_residue"
        })
        data["summary"] = "已经开始填写"
        write_json(delta_path, data)
        denied = run_script("novelctl.py", "prepare-delta", str(self.root), "--chapter", "1")
        self.assertEqual(denied.returncode, 2)
        self.assertIn("already contains work", denied.stderr)

    def test_commit_requires_minimum_handoff_tuple(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "缺少承接", "人物走进房间。")
        data = delta_for(1, "缺少承接", "人物进入房间")
        data["current_patch"].pop("point_of_view")
        failed = commit(self.root, 1, data, expect_ok=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("current_patch.point_of_view is required", failed.stderr)
        self.assertTrue((self.root / ".novel/staging/chapter-0001.md").is_file())
        self.assertFalse((self.root / "chapters/chapter-0001.md").exists())

    def test_scene_bridge_is_persisted_and_invalid_shape_is_rejected(self) -> None:
        init_project(self.root)
        draft(self.root, 1, "门前", "他抬起手，门后传来脚步声。")
        bad = delta_for(1, "门前", "门后出现异动")
        bad["current_patch"] = {"scene_bridge": "broken"}
        failed = commit(self.root, 1, bad, expect_ok=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("scene_bridge must be an object", failed.stderr)

        bridge = {
            "time": "深夜",
            "location": "旧宅门前",
            "pov": "主角",
            "last_action": "他抬起手准备推门",
            "immediate_pressure": "门后脚步正在接近",
            "emotional_residue": "警惕中夹着犹豫",
        }
        good = delta_for(1, "门前", "门后出现异动")
        good["current_patch"].update({
            "current_location": "旧宅门前",
            "point_of_view": "主角",
            "scene_entities": [],
            "current_goal": "判断门后是谁",
            "scene_bridge": bridge,
        })
        commit(self.root, 1, good)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        meta = json.loads((self.root / "state/chapters/chapter-0001.json").read_text(encoding="utf-8"))
        self.assertEqual(current["scene_bridge"], bridge)
        self.assertEqual(meta["scene_bridge"], bridge)
        self.assertEqual(meta["current_location"], "旧宅门前")
        self.assertEqual(meta["point_of_view"], "主角")
        self.assertEqual(meta["current_goal"], "判断门后是谁")
        self.assertEqual(meta["scene_entities"], [])
        context = run_script("build_context.py", str(self.root), "--chapter", "2", check=True)
        packet = (self.root / json.loads(context.stdout)["output"]).read_text(encoding="utf-8")
        self.assertIn("上一章场景交接", packet)
        self.assertIn("门后脚步正在接近", packet)

    def test_context_prioritizes_scene_entities_and_uses_one_full_chapter_plus_tail(self) -> None:
        init_project(self.root)
        template = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        entity_ids = []
        for number in range(1, 13):
            entity_id = f"CHAR-{number:04d}"
            entity = json.loads(json.dumps(template, ensure_ascii=False))
            entity.update({
                "id": entity_id,
                "name": f"角色{number}",
                "created_chapter": 1,
                "notes": (f"ARC-ENTITY-{number}-" * 240),
            })
            write_json(self.root / f"state/entities/characters/{entity_id}.json", entity)
            entity_ids.append(entity_id)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        current.update({
            "latest_chapter": 2,
            "scene_entities": [entity_ids[0]],
            "arc_entities": entity_ids[1:],
            "active_entities": entity_ids,
            "scene_bridge": {
                "time": "清晨", "location": "村口", "pov": entity_ids[0],
                "last_action": "主角抬头", "immediate_pressure": "钟声响起", "emotional_residue": "不安"
            },
        })
        write_json(self.root / "state/current.json", current)
        (self.root / "chapters/chapter-0001.md").write_text(
            "# 第1章 旧事\n\nCH1-BEGIN-MARKER\n" + ("旧正文。" * 900) + "\nCH1-TAIL-MARKER\n",
            encoding="utf-8",
        )
        (self.root / "chapters/chapter-0002.md").write_text(
            "# 第2章 现在\n\nCH2-FULL-MARKER\n" + ("当前正文。" * 300), encoding="utf-8"
        )
        for number in (1, 2):
            write_json(self.root / f"state/chapters/chapter-{number:04d}.json", {
                "schema": 1, "chapter": number, "title": str(number), "summary": f"摘要{number}",
                "entities": [], "events": [], "depends_on_events": [], "outline_node": "NODE-001"
            })
        proc = run_script("build_context.py", str(self.root), "--chapter", "3", "--query", "村口钟声")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        packet = (self.root / payload["output"]).read_text(encoding="utf-8")
        self.assertLessEqual(payload["chars"], 30000)
        self.assertIn("CH2-FULL-MARKER", packet)
        self.assertIn("CH1-TAIL-MARKER", packet)
        self.assertNotIn("CH1-BEGIN-MARKER", packet)
        self.assertIn("角色1", packet)
        self.assertTrue(any("CHAR-" in item for item in payload["optional_omitted"]))

    def test_removed_agent_contract_commands_are_not_exposed(self) -> None:
        init_project(self.root)
        prepare = run_script("novelctl.py", "agent-prepare", str(self.root))
        validate = run_script("novelctl.py", "agent-validate", str(self.root))
        prose = run_script("novelctl.py", "prose-bible", str(self.root))
        self.assertEqual(prepare.returncode, 2)
        self.assertEqual(validate.returncode, 2)
        self.assertEqual(prose.returncode, 2)
        self.assertFalse((SKILL / "assets/agent-task-contract.md").exists())
        self.assertFalse((SCRIPTS / "agent_contract.py").exists())

    def test_lightweight_style_reference_lessons_and_voice_compatibility(self) -> None:
        init_project(self.root)
        style = self.root / "canon/prose-contract.md"
        lessons = self.root / "state/creative-lessons.md"
        style.write_text("# Prose Contract\n\nSTYLE-MARKER\n", encoding="utf-8")
        lessons.write_text("# 创作经验\n\nLESSON-MARKER\n", encoding="utf-8")
        context = run_script("build_context.py", str(self.root), "--chapter", "1", "--query", "当前章人物选择", check=True)
        context_text = (self.root / json.loads(context.stdout)["output"]).read_text(encoding="utf-8")
        self.assertIn("STYLE-MARKER", context_text)
        self.assertIn("LESSON-MARKER", context_text)
        self.assertIn("项目 Prose Contract", context_text)
        self.assertIn("项目创作经验", context_text)
        author_context = run_script(
            "build_context.py", str(self.root), "--chapter", "1", "--role", "author", "--query", "当前章人物选择", check=True
        )
        author_payload = json.loads(author_context.stdout)
        author_text = (self.root / author_payload["output"]).read_text(encoding="utf-8")
        self.assertIn("上下文角色：author", author_text)
        self.assertIn("STYLE-MARKER", author_text)
        reviewer_context = run_script(
            "build_context.py", str(self.root), "--chapter", "1", "--role", "reviewer", "--query", "当前章人物选择", check=True
        )
        reviewer_payload = json.loads(reviewer_context.stdout)
        reviewer_text = (self.root / reviewer_payload["output"]).read_text(encoding="utf-8")
        self.assertNotEqual(json.loads(context.stdout)["output"], reviewer_payload["output"])
        self.assertIn("上下文角色：reviewer", reviewer_text)
        self.assertNotIn("STYLE-MARKER", reviewer_text)
        self.assertNotIn("LESSON-MARKER", reviewer_text)
        template = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        self.assertIsInstance(template["voice"], dict)
        self.assertIn("personal_game", template)
        self.assertIn("pressure_response", template["voice"])
        self.assertIn("words_never_used", template["voice"])
        from common import validate_entity_data
        template.update({"id": "CHAR-0001", "name": "角色甲", "created_chapter": 1})
        self.assertEqual(validate_entity_data(template, "CHAR-0001"), [])
        template["voice"] = "说话简短直接"
        self.assertEqual(validate_entity_data(template, "CHAR-0001"), [])

    def test_stale_handoff_filters_short_term_next_action(self) -> None:
        init_project(self.root)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        current["latest_chapter"] = 20
        write_json(self.root / "state/current.json", current)
        (self.root / "state/session-handoff.md").write_text(
            "---\nschema: 1\nstatus: active\nupdated_at: 2026-01-01T00:00:00Z\nthrough_chapter: 1\ncurrent_arc: ARC-001\nsupersedes:\n---\n"
            "# 会话交接\n\n## 仍有效的创作决定\n\n- LONG-LIVED-DECISION\n\n"
            "## 有效的成功经验\n\n- KEEP-LESSON\n\n## 下一步\n\n- STALE-NEXT-ACTION\n",
            encoding="utf-8",
        )
        proc = run_script("build_context.py", str(self.root), "--chapter", "21")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = (self.root / json.loads(proc.stdout)["output"]).read_text(encoding="utf-8")
        self.assertIn("LONG-LIVED-DECISION", text)
        self.assertIn("KEEP-LESSON", text)
        self.assertNotIn("STALE-NEXT-ACTION", text)
        self.assertIn("已超过短期有效窗口", text)

    def test_quality_scan_reports_style_risks_without_judging(self) -> None:
        init_project(self.root)
        body = (
            "他看见门开了，感觉到紧张，不禁倒吸一口凉气。" * 12
            + "\n\n所谓规则事实上意味着所有人必须服从，因为这是古老历史，因此没有选择。" * 8
            + "\n\n有人来了。"
        )
        for number in (1, 2, 3):
            (self.root / "chapters" / f"chapter-{number:04d}.md").write_text(
                f"# 第{number}章 测试\n\n{body}\n", encoding="utf-8"
            )
        proc = run_script("quality_scan.py", str(self.root), "--recent", "3")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = (self.root / "audits/quality-scan.md").read_text(encoding="utf-8")
        self.assertIn("模板化风险", report)
        self.assertIn("结尾类型", report)
        self.assertIn("不是文学评分器", report)


    def test_quality_scan_ignores_low_density_term_counts_across_long_range(self) -> None:
        init_project(self.root)
        for number in range(1, 31):
            tagged = "他说道。" if number <= 8 else ""
            body = ("风从山坡吹过，草叶顺着一个方向伏下。" * 180) + tagged + f"第{number}个不同结尾。"
            (self.root / f"chapters/chapter-{number:04d}.md").write_text(
                f"# 第{number}章 测试\n\n{body}\n", encoding="utf-8"
            )
        proc = run_script("quality_scan.py", str(self.root), "--recent", "30")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = (self.root / "audits/quality-scan.md").read_text(encoding="utf-8")
        self.assertNotIn("对白标签密度偏高", report)

    def test_installer_rejects_linked_package_source(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink unavailable")
        package = Path(self.temp.name) / "linked-package"
        shutil.copytree(ROOT, package)
        outside = Path(self.temp.name) / "outside-agent.md"
        outside.write_text("outside", encoding="utf-8")
        linked = package / ".claude/agents/linked.md"
        try:
            os.symlink(outside, linked)
        except OSError as exc:
            self.skipTest(str(exc))
        project = Path(self.temp.name) / "linked-install-target"
        project.mkdir()
        proc = subprocess.run(
            ["bash", str(package / "install.sh"), "--scope", "project", "--project-path", str(project)],
            text=True, capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("symbolic link", proc.stderr)

    def test_scale_search_1000_chapters(self) -> None:
        init_project(self.root)
        chapter_dir = self.root / "state" / "chapters"
        for number in range(1, 1001):
            summary = "普通推进"
            entities = []
            if number == 137:
                summary = "留下长期线索与既有约定"
                entities = ["ITEM-0001"]
            write_json(chapter_dir / f"chapter-{number:04d}.json", {
                "schema": 1, "chapter": number, "title": f"第{number}章", "summary": summary,
                "entities": entities, "events": [], "depends_on_events": [],
            })
        start = time.perf_counter()
        results = collect(self.root, "长期线索 既有约定")
        selected = balanced_select(results, 8)
        elapsed = time.perf_counter() - start
        self.assertTrue(any("137" in item.source for item in selected))
        self.assertEqual(len({item.source for item in selected}), len(selected))
        self.assertLessEqual(sum("普通推进" in item.excerpt for item in selected), 1)
        limit = float(os.environ.get("NOVEL_CREATOR_SEARCH_BENCHMARK_SECONDS", "15"))
        self.assertLess(elapsed, limit)



    def test_batch_end_requires_reader_review_and_persists_literary_feedback(self) -> None:
        init_project(self.root, test_batch_size=5)
        for number in range(1, 6):
            draft(self.root, number, f"第{number}步", "人物继续前进。")
        end_data = delta_for(5, "阶段收束", "第一批次结束")
        end_data.update({
            "chapter_function": "mixed",
            "dominant_change": "主角从被动调查转为主动追索",
            "reader_expectation_added": "读者期待下一批揭开幕后入口",
        })
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        batch = current["batch"]
        end_data["current_patch"]["reader_review"] = {
            "reviewed_through_chapter": 5,
            "reason": "batch",
            "verdict": "acceptable",
            "ending_pull": "strong",
            "revision_applied": True,
            "issue_tags": [],
            "highest_value_revision": "",
            "batch_id": batch["batch_id"],
            "batch_start_chapter": batch["start_chapter"],
            "batch_end_chapter": batch["end_chapter"],
        }
        # A batch cannot start committing before both reviews are finalized.
        blocked_first = commit(
            self.root, 1, delta_for(1, "第1步", "完成第1步"),
            expect_ok=False, prepare_review=False,
        )
        self.assertNotEqual(blocked_first.returncode, 0)
        self.assertIn("finalized review record", blocked_first.stderr)
        finalize_batch_review_from_delta(self.root, 5, end_data)
        for number in range(1, 5):
            data = delta_for(number, f"第{number}步", f"完成第{number}步")
            data["chapter_function"] = "advance"
            commit(self.root, number, data)
        # The final chapter still requires its reader summary to match the record.
        missing_reader = delta_for(5, "阶段收束", "第一批次结束")
        missing_reader.update({
            "chapter_function": "mixed",
            "dominant_change": "主角从被动调查转为主动追索",
            "reader_expectation_added": "读者期待下一批揭开幕后入口",
        })
        blocked = commit(self.root, 5, missing_reader, expect_ok=False, prepare_review=False)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("complete First Reader conclusion", blocked.stderr)
        commit(self.root, 5, end_data)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        meta = json.loads((self.root / "state/chapters/chapter-0005.json").read_text(encoding="utf-8"))
        self.assertEqual(current["reader_review"]["reviewed_through_chapter"], 5)
        self.assertEqual(current["reader_review"]["ending_pull"], "strong")
        self.assertEqual(meta["chapter_function"], "mixed")
        self.assertEqual(meta["dominant_change"], "主角从被动调查转为主动追索")
        self.assertEqual(meta["reader_review"], current["reader_review"])

    def test_prepare_delta_scaffolds_batch_reader_review_only_at_batch_end(self) -> None:
        init_project(self.root, test_batch_size=5)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        current["latest_chapter"] = 4
        write_json(self.root / "state/current.json", current)
        draft(self.root, 5, "批次末章", "正文。")
        proc = run_script("prepare_delta.py", str(self.root), "--chapter", "5", check=True)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["batch_end"])
        self.assertTrue(payload["reader_review_required"])
        data = json.loads((self.root / payload["output"]).read_text(encoding="utf-8"))
        self.assertEqual(data["chapter_function"], "")
        self.assertEqual(data["current_patch"]["reader_review"]["reviewed_through_chapter"], 5)
        self.assertEqual(data["current_patch"]["reader_review"]["reason"], "batch")
        self.assertIsNone(data["current_patch"]["reader_review"]["revision_applied"])

    def test_reader_isolation_voice_examples_and_feedback_lessons_contract(self) -> None:
        reader = (ROOT / ".claude/agents/novel-fast-reader-flow.md").read_text(encoding="utf-8")
        self.assertNotIn("TodoWrite", reader)
        self.assertIn("Read(.novel/blind-packets/**)", reader)
        self.assertNotIn("  - TaskStop", reader)
        self.assertIn("五章", reader)
        self.assertIn("综合盲读主席", reader)
        self.assertIn("most_alive", reader)
        self.assertIn("flattest_or_generic", reader)
        reader_tools = reader.split("tools:", 1)[1].split("disallowedTools:", 1)[0]
        self.assertIn("Read(.novel/blind-packets/**)", reader_tools)
        self.assertNotIn("Glob", reader_tools)
        self.assertNotIn("Grep", reader_tools)
        lessons = (ASSETS / "creative-lessons-template.md").read_text(encoding="utf-8")
        self.assertIn("连续两个批次", lessons)
        template = json.loads((ASSETS / "character-template.json").read_text(encoding="utf-8"))
        self.assertIn("voice_examples", template["voice"])
        self.assertIn("voice_anti_examples", template["voice"])
        self.assertIn("inner_monologue_distance", template["voice"])
        self.assertIn("default_misinterpretation", template["voice"])
        self.assertIn("silence_pattern", template["voice"])
        from common import validate_entity_data
        template.update({"id": "CHAR-0001", "name": "角色甲", "created_chapter": 1})
        template["voice"]["voice_examples"] = ["短句一", "短句二", "短句三"]
        self.assertEqual(validate_entity_data(template, "CHAR-0001"), [])
        template["voice"]["voice_examples"].append("第四句")
        self.assertTrue(any("at most 3" in item for item in validate_entity_data(template, "CHAR-0001")))

    def test_audit_warns_when_claude_memory_may_pollute_blind_reading(self) -> None:
        init_project(self.root, relaxed_length=False)
        (self.root / "CLAUDE.md").write_text("幕后真凶与预期转折不应暴露给盲读者。\n", encoding="utf-8")
        proc = run_script("audit_project.py", str(self.root), check=True)
        payload = json.loads(proc.stdout)
        self.assertGreaterEqual(payload["warnings"], 1)
        report = (self.root / payload["output"]).read_text(encoding="utf-8")
        self.assertIn("盲读污染风险", report)
        self.assertIn("CLAUDE.md", report)

    def test_audit_warns_for_overdue_reader_review_and_repetitive_chapter_function(self) -> None:
        init_project(self.root, test_batch_size=5)
        # Commit two real five-chapter review batches, then deliberately stale the
        # current reader pointer so audit can report the overdue cadence.
        for batch_start in (1, 6):
            batch_end = batch_start + 4
            for number in range(batch_start, batch_end + 1):
                draft(self.root, number, f"推进{number}", "人物持续推进目标。")
            current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
            batch = current["batch"]
            end_data = delta_for(batch_end, f"推进{batch_end}", f"推进到第{batch_end}步")
            end_data["chapter_function"] = "advance"
            end_data["dominant_change"] = f"推进到第{batch_end}步"
            end_data["current_patch"]["reader_review"] = {
                "reviewed_through_chapter": batch_end, "reason": "batch",
                "verdict": "acceptable", "ending_pull": "fair",
                "revision_applied": True, "issue_tags": [], "highest_value_revision": "",
                "batch_id": batch["batch_id"],
                "batch_start_chapter": batch["start_chapter"],
                "batch_end_chapter": batch["end_chapter"],
            }
            finalize_batch_review_from_delta(self.root, batch_end, end_data)
            for number in range(batch_start, batch_end):
                data = delta_for(number, f"推进{number}", f"推进到第{number}步")
                data["chapter_function"] = "advance"
                data["dominant_change"] = f"推进到第{number}步"
                commit(self.root, number, data)
            commit(self.root, batch_end, end_data)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        current["reader_review"] = {
            "reviewed_through_chapter": 0, "reason": "", "verdict": "",
            "ending_pull": "", "revision_applied": None,
        }
        write_json(self.root / "state/current.json", current)
        audit = run_script("audit_project.py", str(self.root))
        self.assertEqual(audit.returncode, 0, audit.stderr)
        report = (self.root / "audits/latest-audit.md").read_text(encoding="utf-8")
        self.assertIn("First Reader 盲读节奏已逾期", report)
        self.assertIn("最近章节功能较单一", report)

    def test_alternative_staging_cannot_replace_canonical_and_export_reports_latest(self) -> None:
        init_project(self.root)
        alternative = self.root / ".novel/staging/alternatives/chapter-0001-plan-b.md"
        alternative.parent.mkdir(parents=True, exist_ok=True)
        alternative.write_text("# 第1章 备选\n\n备选正文。\n", encoding="utf-8")
        write_json(self.root / "state/deltas/chapter-0001.json", delta_for(1, "正式", "正式摘要"))
        blocked = run_script("commit_chapter.py", str(self.root), "--chapter", "1")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("chapter prose is missing", blocked.stderr)
        self.assertTrue(alternative.is_file())
        draft(self.root, 1, "正式", "正式正文。")
        commit(self.root, 1, delta_for(1, "正式", "正式摘要"))
        self.assertIn("正式正文", (self.root / "chapters/chapter-0001.md").read_text(encoding="utf-8"))
        self.assertTrue(alternative.is_file())
        exported = run_script("export_novel.py", str(self.root), check=True)
        payload = json.loads(exported.stdout)
        self.assertEqual(payload["latest_chapter"], 1)
        self.assertEqual(payload["chapters"], 1)
        self.assertTrue(payload["sha256"])

    def test_removed_unused_expansion_counter(self) -> None:
        targets = [
            ASSETS / "writing-settings-template.json",
            SCRIPTS / "chapter_stats.py",
            SCRIPTS / "init_project.py",
            ROOT / "README.md",
        ]
        for target in targets:
            self.assertNotIn("maximum_" + "expansion_attempts", target.read_text(encoding="utf-8"), target)



    def test_batch_anchor_supports_existing_chapters_and_future_size_change(self) -> None:
        init_project(self.root, test_batch_size=5)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        current["latest_chapter"] = 2
        current.pop("batch", None)
        write_json(self.root / "state/current.json", current)
        configured = run_script(
            "configure_project.py", str(self.root),
            "--batch-size", "5", "--planning-window", "10", "--batch-start", "3",
            check=True,
        )
        payload = json.loads(configured.stdout)
        self.assertEqual(payload["active_batch"]["start_chapter"], 3)
        self.assertEqual(payload["active_batch"]["end_chapter"], 7)
        for number in range(3, 8):
            draft(self.root, number, f"续写{number}", "人物继续推进。")
        end_delta = delta_for(7, "续写7", "完成已有项目后的首个批次")
        end_delta["current_patch"]["reader_review"] = {
            "reviewed_through_chapter": 7,
            "reason": "batch",
            "verdict": "acceptable",
            "ending_pull": "strong",
            "revision_applied": True,
            "issue_tags": ["middle-section-drag"],
            "highest_value_revision": "压缩中段重复解释",
            "batch_id": payload["active_batch"]["batch_id"],
            "batch_start_chapter": 3,
            "batch_end_chapter": 7,
        }
        finalize_batch_review_from_delta(self.root, 7, end_delta)
        # Review batch size is a permanent five-chapter invariant.
        changed = run_script("configure_project.py", str(self.root), "--batch-size", "3")
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("fixed at 5", changed.stderr)
        commit(self.root, 3, delta_for(3, "续写3", "推进到3"))
        reanchor = run_script(
            "configure_project.py", str(self.root),
            "--batch-size", "5", "--batch-start", "4",
        )
        self.assertNotEqual(reanchor.returncode, 0)
        self.assertIn("cannot re-anchor", reanchor.stderr)
        for number in range(4, 7):
            commit(self.root, number, delta_for(number, f"续写{number}", f"推进到{number}"))
        commit(self.root, 7, end_delta)
        current = json.loads((self.root / "state/current.json").read_text(encoding="utf-8"))
        self.assertEqual(current["batch"]["start_chapter"], 8)
        self.assertEqual(current["batch"]["end_chapter"], 12)
        meta = json.loads((self.root / "state/chapters/chapter-0007.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["batch_start_chapter"], meta["batch_end_chapter"]), (3, 7))
        audit = run_script("audit_project.py", str(self.root), "--allow-gaps")
        self.assertEqual(audit.returncode, 0, audit.stderr)

    def test_batch_review_record_binds_both_reviews_and_final_hashes(self) -> None:
        init_project(self.root, test_batch_size=5)
        for number in range(1, 6):
            draft(self.root, number, f"批次{number}", "正文内容。")
        prepared = run_script("prepare_batch_review.py", str(self.root), "--continuity-risk", "low", check=True)
        record_path = self.root / json.loads(prepared.stdout)["output"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["first_reader"] = {
            "status": "completed", "required_count": 3,
            "completed_readers": ["novel-fast-reader-flow", "novel-fast-reader-character", "novel-fast-reader-hook"],
            "available_readers": ["novel-fast-reader-flow", "novel-fast-reader-character", "novel-fast-reader-hook"],
            "verdict": "acceptable", "ending_pull": "fair",
            "revision_applied": True, "issue_tags": ["emotion-overexplained"],
            "highest_value_revision": "删除重复情绪解释",
        }
        record["continuity"] = {"status": "completed", "checked_by": "main-agent", "blocking_count": 0, "warning_count": 1}
        write_json(record_path, record)
        run_script("finalize_batch_review.py", str(self.root), check=True)
        # Any edit after finalization blocks even the first commit in the batch.
        (self.root / ".novel/staging/chapter-0003.md").write_text("# 第3章 批次3\n\n修改后正文。\n", encoding="utf-8")
        write_json(self.root / "state/deltas/chapter-0001.json", delta_for(1, "批次1", "推进1"))
        blocked = run_script("commit_chapter.py", str(self.root), "--chapter", "1")
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("final hash no longer matches", blocked.stderr)
        run_script("finalize_batch_review.py", str(self.root), check=True)
        for number in range(1, 5):
            commit(self.root, number, delta_for(number, f"批次{number}", f"推进{number}"))
        end_delta = delta_for(5, "批次5", "批次结束")
        end_delta["current_patch"]["reader_review"] = {
            "reviewed_through_chapter": 5,
            "reason": "batch",
            "verdict": "acceptable",
            "ending_pull": "fair",
            "revision_applied": True,
            "issue_tags": ["emotion-overexplained"],
            "highest_value_revision": "删除重复情绪解释",
            "batch_id": 1,
            "batch_start_chapter": 1,
            "batch_end_chapter": 5,
        }
        commit(self.root, 5, end_delta)

    def test_configure_persists_user_length_defaults(self) -> None:
        init_project(self.root, relaxed_length=False)
        configured = run_script(
            "configure_project.py", str(self.root),
            "--min-chars", "3000", "--target-chars", "3500", "--soft-max-chars", "4000",
            check=True,
        )
        payload = json.loads(configured.stdout)
        self.assertEqual(payload["chapter_length"]["minimum_effective_chars"], 3000)
        settings = json.loads((self.root / "state/writing-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["chapter_length"], {
            "minimum_effective_chars": 3000,
            "target_effective_chars": 3500,
            "soft_maximum_effective_chars": 4000,
        })


    def test_skill_bound_wrapper_binds_current_project_and_rejects_workspace_override(self) -> None:
        self.root.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside-workspace"
        blocked = subprocess.run(
            [sys.executable, str(SCRIPTS / "novelctl_skill.py"), "init", str(outside), "--title", "越界"],
            cwd=self.root, text=True, capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, timeout=30,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertFalse(outside.exists())
        self.assertFalse((self.root / "project.md").exists())
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "novelctl_skill.py"), "init", "--title", "绑定项目", "--genre", "通用"],
            cwd=self.root, text=True, capture_output=True, encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.root / "project.md").is_file())

    def test_fixed_review_batch_and_main_agent_tail_production(self) -> None:
        self.root.mkdir(parents=True)
        bad = run_script("init_project.py", str(self.root), "--batch-size", "3")
        self.assertNotEqual(bad.returncode, 0)
        shutil.rmtree(self.root, ignore_errors=True)
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        prepared = run_script("prepare_production.py", str(self.root), "--writers", "2", "--chapters", "12", check=True)
        payload = json.loads(prepared.stdout)
        self.assertEqual(payload["writer_count"], 2)
        self.assertEqual([(a["start_chapter"], a["end_chapter"]) for a in payload["assignments"]], [(1, 5), (6, 10)])
        self.assertEqual(payload["main_agent_tail"]["start_chapter"], 11)
        self.assertEqual(payload["main_agent_tail"]["end_chapter"], 12)
        self.assertEqual(payload["main_agent_tail"]["writer"], "main-agent")

    def test_under_five_production_uses_no_writer_and_final_tail_blind_packet(self) -> None:
        init_project(self.root, relaxed_length=False, test_batch_size=5)
        prepared = run_script("prepare_production.py", str(self.root), "--chapters", "3", check=True)
        payload = json.loads(prepared.stdout)
        self.assertEqual(payload["writer_count"], 0)
        self.assertEqual(payload["assignments"], [])
        self.assertEqual(payload["main_agent_tail"]["chapter_count"], 3)
        for number in range(1, 4):
            draft(self.root, number, f"尾声{number}", f"尾声正文{number}。")
        reviewed = run_script(
            "prepare_batch_review.py", str(self.root), "--continuity-risk", "low", "--final-tail-count", "3", check=True
        )
        record = json.loads((self.root / json.loads(reviewed.stdout)["output"]).read_text(encoding="utf-8"))
        self.assertEqual(record["review_kind"], "final_tail")
        packet = self.root / record["blind_packet"]["path"]
        self.assertTrue(packet.is_file())
        self.assertIn("第3章", packet.read_text(encoding="utf-8"))


FAST_TESTS = {
    "test_skill_bound_wrapper_binds_current_project_and_rejects_workspace_override",
    "test_fixed_review_batch_and_main_agent_tail_production",
    "test_under_five_production_uses_no_writer_and_final_tail_blind_packet",
    "test_basic_commit_context_audit_export",
    "test_commit_requires_minimum_handoff_tuple",
    "test_default_length_settings_and_stats_metric",
    "test_entity_candidates_are_advisory_and_do_not_modify_state",
    "test_invalid_length_settings_are_reported_without_traceback",
    "test_novel_creator_skill_and_agent_task_cards",
    "test_parallel_production_manifest_and_status",
    "test_persisted_writer_risk_forces_high_continuity_review",
    "test_reader_panel_requires_configured_count",
    "test_two_reader_panel_defaults_to_flow_and_hook",
    "test_high_risk_batch_requires_fast_continuity_reviewer",
    "test_prepare_delta_scaffolds_title_progress_questions_and_scene_bridge",
    "test_prepare_delta_scaffolds_batch_reader_review_only_at_batch_end",
    "test_reader_isolation_voice_examples_and_feedback_lessons_contract",
    "test_removed_unused_expansion_counter",
    "test_short_chapter_is_blocked_and_override_is_recorded",
    "test_single_wrapper_dispatches_trusted_commands",
    "test_alternative_staging_cannot_replace_canonical_and_export_reports_latest",
}
BENCHMARK_TESTS = {"test_scale_search_1000_chapters"}


def category_for_test(name: str) -> str:
    if name in BENCHMARK_TESTS:
        return "benchmark"
    if any(token in name for token in (
        "rewrite", "installer", "seal_baselines", "revision_archive",
    )):
        return "rewrite-install"
    if any(token in name for token in (
        "symlink", "hard_link", "linked_", "path_traversal", "filename_must_match",
        "recovery_never_expands", "committed_cleanup",
    )):
        return "security"
    if any(token in name for token in (
        "transaction", "recovery", "event_append", "arc_update", "writer_staging",
        "uncommitted_formal", "commit_rechecks",
    )):
        return "transactions"
    if any(token in name for token in (
        "context", "handoff", "memory", "required_context", "current_arc", "relevant_arc",
    )):
        return "context"
    if any(token in name for token in (
        "reader", "chapter_function", "batch_end", "prepare_delta", "quality", "voice",
        "length", "audit_warns", "creative_lessons",
    )):
        return "literary"
    if any(token in name for token in (
        "entity", "event", "knowledge", "state", "scene_bridge", "minimum_handoff",
        "malformed_entity", "relationship", "quest",
    )):
        return "continuity"
    return "core"


def selected_test_names(suite_name: str) -> list[str]:
    all_names = set(unittest.defaultTestLoader.getTestCaseNames(NovelSkillTests))
    if suite_name == "fast":
        selected = FAST_TESTS
    elif suite_name == "benchmark":
        selected = BENCHMARK_TESTS
    elif suite_name in {"core", "context", "literary", "continuity", "transactions", "security", "rewrite-install"}:
        selected = {name for name in all_names if category_for_test(name) == suite_name}
    elif suite_name == "state":
        selected = {name for name in all_names if category_for_test(name) in {"context", "literary", "continuity"}}
    elif suite_name == "slow":
        selected = all_names - FAST_TESTS - BENCHMARK_TESTS
    elif suite_name == "full":
        selected = all_names
    else:
        raise ValueError(f"unknown suite: {suite_name}")
    missing = selected - all_names
    if missing:
        raise ValueError("suite references missing tests: " + ", ".join(sorted(missing)))
    return sorted(selected)


def run_suite(suite_name: str, *, isolated: bool | None = None) -> int:
    names = selected_test_names(suite_name)
    if isolated is None:
        isolated = False
    started = time.perf_counter()
    if not isolated:
        suite = unittest.TestSuite(NovelSkillTests(name) for name in names)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        print(json.dumps({
            "suite": suite_name, "tests": len(names),
            "failures": len(result.failures) + len(result.errors),
            "seconds": round(time.perf_counter() - started, 2),
        }, ensure_ascii=False))
        return 0 if result.wasSuccessful() else 1

    failures: list[str] = []
    for name in names:
        print(f"[RUN] {name}", flush=True)
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--single", name],
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            start_new_session=True,
        )
        try:
            returncode = proc.wait(timeout=75)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
            failures.append(f"{name}: timeout")
            print(f"[FAIL] {name}: timeout", flush=True)
            continue
        if returncode != 0:
            failures.append(name)
            print(f"[FAIL] {name}", flush=True)
        else:
            print(f"[PASS] {name}", flush=True)
    print(json.dumps({
        "suite": suite_name, "tests": len(names), "failures": failures,
        "seconds": round(time.perf_counter() - started, 2),
    }, ensure_ascii=False))
    return 1 if failures else 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--single":
        suite = unittest.TestSuite([NovelSkillTests(sys.argv[2])])
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    parser = argparse.ArgumentParser(description="Run Novel Creator regression suites.")
    parser.add_argument(
        "--suite",
        choices=("fast", "core", "context", "literary", "continuity", "state", "transactions", "security", "rewrite-install", "slow", "full", "benchmark"),
        default="fast",
    )
    parser.add_argument("--isolated", action="store_true", help="Run each selected test in a fresh Python process")
    args = parser.parse_args()
    return run_suite(args.suite, isolated=True if args.isolated else None)


if __name__ == "__main__":
    raise SystemExit(main())
