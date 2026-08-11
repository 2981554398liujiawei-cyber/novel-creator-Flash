#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from batch_state import load_active_batch
from chapter_stats import analyze_chapter_text, load_length_settings
from common import load_json, read_text, safe_workspace_path, sha256_text, title_chapter_number, validate_workspace_layout
from production_state import production_manifest_relative


def main() -> int:
    parser = argparse.ArgumentParser(description="Check every parallel writer output for presence, safety, chapter number and deterministic length.")
    parser.add_argument("workspace", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        current = load_json(root / "state/current.json", required=True)
        if not isinstance(current, dict):
            raise ValueError("state/current.json must be an object")
        batch = load_active_batch(root, current)
        manifest_path = safe_workspace_path(root, production_manifest_relative(batch), allow_missing=False)
        manifest = load_json(manifest_path, required=True)
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise ValueError("production manifest must be a schema 1 object")
        for key in ("batch_id", "start_chapter", "end_chapter", "batch_size"):
            if manifest.get(key) != batch.get(key):
                raise ValueError(f"production manifest {key} does not match the active batch")
        settings = load_length_settings(root)
        results = []
        all_ready = True
        for assignment in manifest.get("assignments", []):
            if not isinstance(assignment, dict):
                raise ValueError("production assignment must be an object")
            chapter = assignment.get("chapter")
            output = assignment.get("output")
            writer = assignment.get("writer")
            if not isinstance(chapter, int) or isinstance(chapter, bool) or not isinstance(output, str):
                raise ValueError("production assignment chapter/output is invalid")
            path = safe_workspace_path(root, output, allow_missing=True)
            item = {"chapter": chapter, "writer": writer, "output": output, "exists": path.is_file()}
            if not path.is_file():
                item["status"] = "missing"
                all_ready = False
            else:
                if getattr(path.stat(), "st_nlink", 1) != 1:
                    raise ValueError(f"hard-linked writer output is not allowed: {output}")
                text = read_text(path, required=True)
                heading_chapter = title_chapter_number(text)
                item["heading_chapter"] = heading_chapter
                if heading_chapter != chapter:
                    item["status"] = "wrong_chapter_heading"
                    all_ready = False
                stats = analyze_chapter_text(text, settings)
                item.update(stats)
                item["sha256"] = sha256_text(text.rstrip() + "\n")
                if not stats["passes_minimum"]:
                    all_ready = False
            results.append(item)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({
        "batch": f"{batch['start_chapter']}-{batch['end_chapter']}",
        "all_ready": all_ready,
        "outputs": results,
        "next": "Main Claude must integrate these raw drafts sequentially into canonical staging; raw drafts are never committable.",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
