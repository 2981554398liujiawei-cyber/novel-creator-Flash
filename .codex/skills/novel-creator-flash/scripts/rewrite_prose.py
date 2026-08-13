#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

from common import (
    atomic_write_json,
    atomic_write_text,
    chapter_filename,
    chapter_meta_filename,
    file_prefix_sha256,
    first_heading,
    load_json,
    read_text,
    remove_transaction_backup,
    safe_workspace_path,
    sha256_bytes,
    sha256_text,
    title_chapter_number,
    utc_timestamp,
    validate_transaction_journal,
    validate_workspace_layout,
    workspace_lock,
)
from commit_chapter import backup_file, restore_transaction


def json_text(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Replace committed prose while preserving the old revision and requiring a fact review.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--draft", default="")
    parser.add_argument("--keep-draft", action="store_true")
    parser.add_argument("--review-level", choices=("prose","semantic","structural"), default="semantic", help="prose=wording only; semantic=facts/knowledge/bridge may change; structural=event structure or downstream canon may change")
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    validate_workspace_layout(root)
    chapter = args.chapter
    draft = safe_workspace_path(root, args.draft or f"drafts/{chapter_filename(chapter)}", allow_missing=False)
    final = safe_workspace_path(root, f"chapters/{chapter_filename(chapter)}", allow_missing=False)
    meta_path = safe_workspace_path(root, f"state/chapters/{chapter_meta_filename(chapter)}", allow_missing=False)
    journal_path = safe_workspace_path(root, ".novel/transaction.json", allow_missing=True)

    with workspace_lock(root):
        validate_workspace_layout(root)
        if journal_path.exists():
            parser.error("an unfinished transaction exists; recover it first")
        if not final.is_file() or not meta_path.is_file():
            parser.error("only a committed chapter can be rewritten")
        draft_raw = read_text(draft, required=True)
        prose = draft_raw.rstrip() + "\n"
        draft_sha = sha256_text(draft_raw)
        old_prose = read_text(final, required=True)
        if title_chapter_number(prose) != chapter:
            parser.error("draft title chapter number does not match the target chapter")
        if args.review_level == "prose" and first_heading(prose) != first_heading(old_prose):
            parser.error("prose-only rewrite must preserve the exact chapter heading; use --review-level semantic or structural otherwise")
        meta = load_json(meta_path, required=True)
        if not isinstance(meta, dict):
            parser.error("chapter metadata must be an object")
        if meta.get("prose_rewrite_review_required"):
            parser.error("the previous prose rewrite is still awaiting fact review")
        old_revision = meta.get("revision", 1)
        if not isinstance(old_revision, int) or isinstance(old_revision, bool) or old_revision < 1:
            parser.error("chapter revision must be a positive integer")
        history = meta.get("revision_history", [])
        if not isinstance(history, list):
            parser.error("chapter revision_history must be a list")

        revision_root = safe_workspace_path(root, f"revisions/chapter-{chapter:04d}", allow_missing=True)
        revision_root.mkdir(parents=True, exist_ok=True)
        # Revalidate after creation in case a platform-specific reparse point appeared.
        validate_workspace_layout(root)
        archived_prose = safe_workspace_path(root, f"revisions/chapter-{chapter:04d}/revision-{old_revision:04d}.md", allow_missing=True)
        archived_meta = safe_workspace_path(root, f"revisions/chapter-{chapter:04d}/revision-{old_revision:04d}.json", allow_missing=True)
        if archived_prose.exists() or archived_meta.exists():
            parser.error(f"revision archive already exists for revision {old_revision}; audit the revision chain before retrying")

        old_meta_text = json_text(meta)
        revision_record = {
            "revision": old_revision,
            "archived_at": utc_timestamp(),
            "archive_prose": archived_prose.relative_to(root).as_posix(),
            "archive_metadata": archived_meta.relative_to(root).as_posix(),
            "prose_sha256": sha256_bytes(old_prose.encode("utf-8")),
            "metadata_sha256": sha256_bytes(old_meta_text.encode("utf-8")),
        }

        transaction_id = f"prose-rewrite-{chapter:04d}-{uuid.uuid4().hex[:10]}"
        backup = safe_workspace_path(root, f".novel/backups/{transaction_id}", allow_missing=True)
        backup.mkdir(parents=True, exist_ok=False)
        events_path = safe_workspace_path(root, "state/events/events.jsonl", allow_missing=False)
        events_size = events_path.stat().st_size
        journal = {
            "schema": 1,
            "transaction_id": transaction_id,
            "status": "applying",
            "chapter": chapter,
            "created_at": utc_timestamp(),
            "backup_dir": backup.relative_to(root).as_posix(),
            "events_size": events_size,
            "events_prefix_sha256": file_prefix_sha256(events_path, events_size),
            "files": [
                backup_file(root, backup, final),
                backup_file(root, backup, meta_path),
                backup_file(root, backup, archived_prose),
                backup_file(root, backup, archived_meta),
            ],
        }
        atomic_write_json(journal_path, journal)
        validate_transaction_journal(root, journal, require_backups=True)
        try:
            atomic_write_text(archived_prose, old_prose)
            atomic_write_text(archived_meta, old_meta_text)
            next_history = list(history) + [revision_record]
            meta["prose_sha256"] = sha256_text(prose)
            meta["revision"] = old_revision + 1
            meta["revision_history"] = next_history
            meta["prose_rewritten_at"] = utc_timestamp()
            meta["prose_rewrite_review_required"] = True
            meta["rewrite_review"] = {
                "status": "pending",
                "previous_revision": old_revision,
                "archive_record_revision": old_revision,
                "new_prose_sha256": sha256_text(prose),
                "level": args.review_level,
                "reviewed_hashes": None,
                "structural_state_reconciled": False,
            }
            atomic_write_json(meta_path, meta)
            atomic_write_text(final, prose)
        except Exception:
            rollback_errors = restore_transaction(root, journal)
            if rollback_errors:
                journal["status"] = "dirty"
                journal["rollback_errors"] = rollback_errors
                atomic_write_json(journal_path, journal)
            else:
                journal_path.unlink(missing_ok=True)
                try:
                    remove_transaction_backup(root, backup, ignore_missing=True)
                except (OSError, ValueError):
                    pass
            raise
        journal["status"] = "committed"
        journal["committed_at"] = utc_timestamp()
        atomic_write_json(journal_path, journal)
        journal_path.unlink()
        try:
            remove_transaction_backup(root, backup, ignore_missing=True)
        except (OSError, ValueError):
            pass
        if not args.keep_draft and draft.exists() and sha256_text(read_text(draft, required=True)) == draft_sha:
            draft.unlink()
        print(json.dumps({
            "rewritten": True,
            "chapter": chapter,
            "mode": f"rewrite-{args.review_level}-pending-review",
            "revision": meta["revision"],
            "review_required": True,
            "archive": archived_prose.relative_to(root).as_posix(),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
