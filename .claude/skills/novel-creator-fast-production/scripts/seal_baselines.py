#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from common import (
    atomic_write_bytes,
    atomic_write_json,
    baseline_path,
    build_baseline_record,
    ensure_no_symlink_chain,
    load_json,
    normalize_entity_id,
    require_int,
    validate_baseline_record,
    validate_entity_data,
    validate_workspace_layout,
    workspace_lock,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Seal immutable entity history for an imported or upgraded project after manual review.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--i-reviewed-history", action="store_true")
    args = parser.parse_args()
    if not args.i_reviewed_history:
        parser.error("review created_chapter and all initial_* fields, then pass --i-reviewed-history")
    root = Path(args.workspace).resolve(strict=True)
    validate_workspace_layout(root)
    created = 0
    verified = 0

    with workspace_lock(root):
        # Validate and prepare every write before changing any file. This prevents a
        # late schema error from leaving half the project sealed.
        baseline_updates: dict[Path, dict[str, Any]] = {}
        meta_updates: dict[Path, dict[str, Any]] = {}
        for path in sorted((root / "state" / "entities").glob("*/*.json")):
            ensure_no_symlink_chain(path, root, allow_missing=False)
            data = load_json(path, required=True)
            if not isinstance(data, dict):
                parser.error(f"entity must be an object: {path.relative_to(root)}")
            entity_id = normalize_entity_id(str(data.get("id", path.stem)))
            problems = validate_entity_data(data, entity_id)
            if problems:
                parser.error(f"invalid entity {entity_id}: " + "; ".join(problems))
            target = baseline_path(root, entity_id)
            existing = load_json(target, default=None)
            record = build_baseline_record(data)
            if existing is None:
                baseline_updates[target] = record
                created += 1
            else:
                problems = validate_baseline_record(existing, data)
                if problems:
                    parser.error(f"baseline conflict for {entity_id}: " + "; ".join(problems))
                record = existing
                verified += 1
            chapter = require_int(data.get("created_chapter"), f"{entity_id}.created_chapter", minimum=1)
            meta_path = root / "state" / "chapters" / f"chapter-{chapter:04d}.json"
            if meta_path.is_file():
                if meta_path not in meta_updates:
                    meta = load_json(meta_path, required=True)
                    if not isinstance(meta, dict):
                        parser.error(f"chapter metadata must be an object: {meta_path.relative_to(root)}")
                    hashes = meta.get("baseline_hashes", {})
                    if not isinstance(hashes, dict):
                        parser.error(f"baseline_hashes must be an object: {meta_path.relative_to(root)}")
                    meta = dict(meta)
                    meta["baseline_hashes"] = dict(hashes)
                    meta_updates[meta_path] = meta
                meta_updates[meta_path]["baseline_hashes"][entity_id] = record["fields_sha256"]

        writes: list[tuple[Path, dict[str, Any]]] = [*baseline_updates.items(), *meta_updates.items()]
        originals: dict[Path, bytes | None] = {path: path.read_bytes() if path.exists() else None for path, _ in writes}
        applied: list[Path] = []
        try:
            for path, data in writes:
                atomic_write_json(path, data)
                applied.append(path)
        except Exception:
            for path in reversed(applied):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_bytes(path, original)
            raise

    print(json.dumps({"sealed": True, "created": created, "verified": verified, "updated_chapters": len(meta_updates)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
