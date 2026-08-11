#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from common import (
    ENTITY_DIRS,
    chapter_filename,
    chapter_meta_filename,
    ensure_no_symlink_chain,
    extract_entity_ids,
    load_json,
    read_text,
    safe_workspace_path,
    validate_workspace_layout,
)

BRACKET_TERM_RE = re.compile(r"[【〔]([^】〕]{2,24})[】〕]")
PLACE_RE = re.compile(
    r"(?:抵达|来到|前往|离开|进入|走进|穿过|向|往|从|在|到|去)"
    r"(?:了|着|过)?"
    r"([\u3400-\u9fff]{2,8}(?:新手村|仓库|码头|广场|村|镇|城|塔|谷|山|河|湖|岛|林|矿|洞|坡|宫|殿|院|楼|店|巷|街))"
    r"(?=附近|周围|里面|内|外|前|后|旁|方向|深处|尽头|[，。！？；：\s])"
)
SPEAKER_RE = re.compile(
    r"(?:^|[，。！？；：\n\r‘’“”\"'])"
    r"([\u3400-\u9fff]{2,4})"
    r"(?:低声|轻声|沉声|冷声|笑着|忽然)?"
    r"(?:说|问|答|道|喊|叫)"
    r"(?=[，。！？；：‘’“”\"'])"
)
TERM_PUNCTUATION = set("，。！？；：,.!?;:\n\r\t")
GENERIC_TERMS = {
    "新手村", "村口", "城里", "城外", "门口", "山上", "山下", "河边", "湖边", "树林", "矿洞",
    "男人", "女人", "老人", "少年", "少女", "系统", "众人", "对方", "有人", "声音", "主人公",
    "低声", "轻声", "沉声", "冷声",
}


def _contains(text: str, needle: str) -> bool:
    if not needle:
        return False
    if any("\u3400" <= char <= "\u9fff" for char in needle):
        return needle in text
    return needle.casefold() in text.casefold()


def _excerpt(text: str, term: str, radius: int = 36) -> str:
    index = text.find(term)
    if index < 0:
        index = text.casefold().find(term.casefold())
    if index < 0:
        return ""
    start = max(0, index - radius)
    end = min(len(text), index + len(term) + radius)
    return re.sub(r"\s+", " ", text[start:end]).strip()



def _valid_candidate(term: str) -> bool:
    value = term.strip()
    if not 2 <= len(value) <= 16:
        return False
    if value in GENERIC_TERMS:
        return False
    if any(char in TERM_PUNCTUATION for char in value):
        return False
    return not value.isdigit()


def _load_entities(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    entities: list[dict[str, Any]] = []
    warnings: list[str] = []
    for directory in ENTITY_DIRS.values():
        base = root / "state" / "entities" / directory
        ensure_no_symlink_chain(base, root, allow_missing=True)
        if not base.exists():
            continue
        for path in sorted(base.glob("*.json")):
            try:
                ensure_no_symlink_chain(path, root, allow_missing=False)
                data = load_json(path, required=True)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                warnings.append(f"无法读取 {path.relative_to(root).as_posix()}: {exc}")
                continue
            if not isinstance(data, dict):
                warnings.append(f"实体文件不是 JSON 对象: {path.relative_to(root).as_posix()}")
                continue
            entity_id = str(data.get("id", "")).strip().upper()
            name = str(data.get("name", "")).strip()
            aliases = data.get("aliases", [])
            if not isinstance(aliases, list):
                aliases = []
            labels = [name, *(str(item).strip() for item in aliases if isinstance(item, str))]
            labels = [label for label in labels if len(label) >= 2]
            if entity_id and labels:
                entities.append({"id": entity_id, "name": name or labels[0], "labels": labels})
    return entities, warnings


def _delta_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    data = load_json(path, required=True)
    if not isinstance(data, dict):
        raise ValueError("delta must be a JSON object")
    return extract_entity_ids(json.dumps(data, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report non-blocking entity mentions and possible unsynchronized names from one chapter."
    )
    parser.add_argument("workspace", nargs="?", default=".")
    parser.add_argument("--chapter", type=int, required=True)
    parser.add_argument("--file", default="", help="Optional chapter or staging file under the workspace")
    parser.add_argument("--delta", default="", help="Optional delta JSON under the workspace")
    args = parser.parse_args()

    root = Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
    except ValueError as exc:
        parser.error(str(exc))
    if args.chapter < 1:
        parser.error("chapter must be positive")

    if args.file:
        try:
            prose_path = safe_workspace_path(root, args.file, allow_missing=False)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        staging = safe_workspace_path(root, f".novel/staging/{chapter_filename(args.chapter)}", allow_missing=True)
        formal = safe_workspace_path(root, f"chapters/{chapter_filename(args.chapter)}", allow_missing=True)
        prose_path = staging if staging.is_file() else formal
    if not prose_path.is_file():
        parser.error(f"chapter prose is missing: {prose_path.relative_to(root).as_posix()}")

    delta_path = safe_workspace_path(
        root,
        args.delta or f"state/deltas/{chapter_meta_filename(args.chapter)}",
        allow_missing=True,
    )
    try:
        prose = read_text(prose_path, required=True)
        indexed_ids = _delta_ids(delta_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    entities, warnings = _load_entities(root)
    known_mentions: list[dict[str, Any]] = []
    known_labels: set[str] = set()
    for entity in entities:
        matched = next((label for label in entity["labels"] if _contains(prose, label)), None)
        known_labels.update(entity["labels"])
        if matched:
            known_mentions.append({
                "id": entity["id"],
                "name": entity["name"],
                "matched_as": matched,
                "indexed_in_delta": entity["id"] in indexed_ids,
                "evidence": _excerpt(prose, matched),
            })

    possible_terms: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(term: str, kind: str) -> None:
        value = term.strip()
        key = (kind, value)
        if key in seen or not _valid_candidate(value):
            return
        if any(_contains(value, label) or _contains(label, value) for label in known_labels):
            return
        seen.add(key)
        possible_terms.append({"term": value, "kind": kind, "evidence": _excerpt(prose, value)})

    for match in BRACKET_TERM_RE.finditer(prose):
        add(match.group(1), "named_item_skill_or_concept")
    for match in PLACE_RE.finditer(prose):
        add(match.group(1), "possible_location")
    for match in SPEAKER_RE.finditer(prose):
        add(match.group(1), "possible_character")

    missing = [item for item in known_mentions if not item["indexed_in_delta"]]
    result = {
        "advisory": True,
        "blocking": False,
        "chapter": args.chapter,
        "source": prose_path.relative_to(root).as_posix(),
        "delta": delta_path.relative_to(root).as_posix() if delta_path.exists() else None,
        "known_mentions": known_mentions,
        "possibly_missing_from_delta": missing,
        "possible_new_terms": possible_terms[:20],
        "warnings": warnings,
        "note": "候选仅供主 Claude 审稿和状态整理时参考；不得自动创建实体，也不得仅凭本报告阻断提交。",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
