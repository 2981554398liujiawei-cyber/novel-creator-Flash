#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from common import (
    atomic_write_text,
    chapter_meta_filename,
    list_chapters,
    load_json,
    output_under,
    read_text,
    sha256_text,
    title_chapter_number,
    validate_revision_history,
    validate_workspace_layout,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export committed novel chapters in numeric order.")
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--output", default="novel.md")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--allow-gaps", action="store_true")
    parser.add_argument("--allow-uncommitted", action="store_true")
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
    except ValueError as exc:
        parser.error(str(exc))
    chapters = list_chapters(root)
    if not chapters:
        parser.error("no committed chapters found")
    numbers = [number for number, _ in chapters]
    if not args.allow_gaps:
        missing = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
        if numbers[0] != 1 or missing:
            parser.error(f"chapter sequence has gaps or does not begin at 1: first={numbers[0]}, missing={missing[:20]}")

    parts: list[str] = []
    for number, path in chapters:
        text = read_text(path, required=True).strip()
        if title_chapter_number(text) != number:
            parser.error(f"chapter title number does not match filename: {path.name}")
        meta = load_json(root / "state" / "chapters" / chapter_meta_filename(number), default=None)
        if not args.allow_uncommitted:
            if not isinstance(meta, dict):
                parser.error(f"missing committed metadata for chapter {number}")
            if str(meta.get("prose_sha256", "")) not in {sha256_text(text + "\n"), sha256_text(text)}:
                parser.error(f"chapter {number} differs from committed metadata")
            if meta.get("prose_rewrite_review_required"):
                parser.error(f"chapter {number} has an unconfirmed prose rewrite")
            revision_errors = validate_revision_history(root, number, meta)
            if revision_errors:
                parser.error("; ".join(revision_errors))
        parts.append(text)

    output = output_under(root, "exports", args.output, "novel.md")
    if output.exists() and not args.overwrite:
        parser.error("output exists; use --overwrite")
    atomic_write_text(output, "\n\n---\n\n".join(parts).rstrip() + "\n")
    print(json.dumps({
        "exported": True,
        "chapters": len(parts),
        "latest_chapter": numbers[-1],
        "output": output.relative_to(root).as_posix(),
        "sha256": sha256_text(read_text(output, required=True)),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
