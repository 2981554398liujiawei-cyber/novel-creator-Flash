#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from common import (
    atomic_write_json,
    chapter_filename,
    chapter_meta_filename,
    ensure_no_symlink_chain,
    load_json,
    read_text,
    sha256_text,
    utc_timestamp,
    validate_workspace_layout,
    workspace_lock,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Confirm that a prose replacement did not require structured state changes.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--note", required=True)
    args = parser.parse_args()
    if len(args.note.strip()) < 12:
        parser.error("--note must briefly state what facts were checked (at least 12 characters)")
    root = Path(args.workspace).resolve(strict=True)
    validate_workspace_layout(root)
    with workspace_lock(root):
        validate_workspace_layout(root)
        if (root / ".novel" / "transaction.json").exists():
            parser.error("an unfinished transaction exists; recover it first")
        meta_path = root / "state" / "chapters" / chapter_meta_filename(args.chapter)
        prose_path = root / "chapters" / chapter_filename(args.chapter)
        meta = load_json(meta_path, required=True)
        if not isinstance(meta, dict) or not meta.get("prose_rewrite_review_required"):
            parser.error("chapter has no pending prose rewrite review")
        prose = read_text(prose_path, required=True)
        if str(meta.get("prose_sha256", "")) != sha256_text(prose):
            parser.error("chapter prose changed after rewrite; review the current file before confirming")
        review = meta.get("rewrite_review", {}) if isinstance(meta.get("rewrite_review"), dict) else {}
        previous_revision = review.get("archive_record_revision")
        history = meta.get("revision_history", [])
        record = next((item for item in history if isinstance(item, dict) and item.get("revision") == previous_revision), None)
        if record is None:
            parser.error("previous revision record is missing")
        for path_field, hash_field in (("archive_prose", "prose_sha256"), ("archive_metadata", "metadata_sha256")):
            path = root / str(record.get(path_field, ""))
            ensure_no_symlink_chain(path, root, allow_missing=False)
            try:
                path.resolve().relative_to((root / "revisions").resolve())
            except ValueError:
                parser.error("revision archive path is outside revisions/")
            if not path.is_file():
                parser.error(f"previous revision archive is missing: {path_field}")
            if hashlib.sha256(path.read_bytes()).hexdigest() != str(record.get(hash_field, "")):
                parser.error(f"previous revision archive hash is invalid: {path_field}")
        review.update({"status": "confirmed", "confirmed_at": utc_timestamp(), "note": args.note.strip()})
        meta["rewrite_review"] = review
        meta["prose_rewrite_review_required"] = False
        atomic_write_json(meta_path, meta)
        print(json.dumps({"confirmed": True, "chapter": args.chapter, "revision": meta.get("revision")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
