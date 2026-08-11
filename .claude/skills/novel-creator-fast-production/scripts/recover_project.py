#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import load_json, remove_transaction_backup, validate_transaction_journal, validate_workspace_layout, workspace_lock
from commit_chapter import restore_transaction


def main() -> int:
    parser = argparse.ArgumentParser(description="Recover an interrupted chapter transaction or finalize committed cleanup.")
    parser.add_argument("workspace", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    validate_workspace_layout(root)
    journal_path = root / ".novel" / "transaction.json"

    with workspace_lock(root):
        validate_workspace_layout(root)
        if not journal_path.exists():
            print(json.dumps({"recovered": False, "reason": "no unfinished transaction"}, ensure_ascii=False))
            return 0
        try:
            journal = load_json(journal_path, required=True)
        except Exception as exc:
            print(json.dumps({"recovered": False, "reason": "transaction journal is unreadable", "error": str(exc)}, ensure_ascii=False))
            return 2
        status = str(journal.get("status", "")) if isinstance(journal, dict) else ""
        try:
            backup_root, _ = validate_transaction_journal(
                root,
                journal,
                require_backups=status in {"applying", "dirty"},
            )
        except ValueError as exc:
            print(json.dumps({
                "recovered": False,
                "reason": "invalid transaction journal; no files were changed",
                "error": str(exc),
            }, ensure_ascii=False))
            return 2

        if status in {"committed", "cleanup_pending"}:
            # A committed transaction no longer needs rollback material. Remove the blocker first;
            # stale backup data is only garbage collection.
            journal_path.unlink(missing_ok=True)
            cleanup_warning = ""
            try:
                remove_transaction_backup(root, backup_root, ignore_missing=True)
            except (OSError, ValueError) as exc:
                cleanup_warning = str(exc)
            print(json.dumps({
                "recovered": True,
                "action": "finalized committed cleanup",
                "chapter": journal.get("chapter"),
                "cleanup_warning": cleanup_warning,
            }, ensure_ascii=False))
            return 0

        errors = restore_transaction(root, journal)
        if errors:
            print(json.dumps({"recovered": False, "reason": "rollback incomplete", "errors": errors}, ensure_ascii=False))
            return 2
        journal_path.unlink(missing_ok=True)
        cleanup_warning = ""
        try:
            remove_transaction_backup(root, backup_root, ignore_missing=True)
        except (OSError, ValueError) as exc:
            cleanup_warning = str(exc)
        print(json.dumps({
            "recovered": True,
            "action": "rolled back",
            "chapter": journal.get("chapter"),
            "cleanup_warning": cleanup_warning,
        }, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
