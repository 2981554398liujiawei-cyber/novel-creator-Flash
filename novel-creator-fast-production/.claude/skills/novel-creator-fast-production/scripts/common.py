#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

CHAPTER_FILE_RE = re.compile(r"^chapter-(\d+)\.md$", re.I)
ENTITY_ID_RE = re.compile(r"(?:CHAR|LOC|ITEM|QUEST|FS|REL)-\d{3,}", re.I)
EVENT_ID_RE = re.compile(r"EVT-(\d{4,})-(\d{3,})", re.I)
ARC_ID_RE = re.compile(r"ARC-\d{3,}", re.I)
NODE_ID_RE = re.compile(r"NODE-\d{3,}", re.I)
TRANSACTION_ID_RE = re.compile(r"(?:commit|prose-rewrite)-\d{4,}-[0-9a-f]{10}")
BASELINE_FILE_RE = re.compile(r"(?:CHAR|LOC|ITEM|QUEST|FS|REL)-\d{3,}\.json", re.I)

ENTITY_DIRS = {
    "characters": "characters",
    "locations": "locations",
    "items": "items",
    "quests": "quests",
    "foreshadows": "foreshadows",
    "relationships": "relationships",
}
ENTITY_PREFIX_TO_DIR = {
    "CHAR": "characters",
    "LOC": "locations",
    "ITEM": "items",
    "QUEST": "quests",
    "FS": "foreshadows",
    "REL": "relationships",
}
IMMUTABLE_BASELINE_FIELDS = {
    "CHAR": {"created_chapter", "initial_status", "initial_location", "initial_knowledge", "initial_skills"},
    "ITEM": {"created_chapter", "initial_status", "initial_owner"},
    "QUEST": {"created_chapter", "initial_status"},
    "REL": {"created_chapter", "initial_status", "initial_stage"},
    "LOC": {"created_chapter", "initial_status"},
    "FS": {"created_chapter", "initial_status"},
}

MANAGED_DIRECTORIES = (
    ".novel",
    ".novel/backups",
    ".novel/cleanup-pending",
    ".novel/staging",
    ".novel/production",
    "canon",
    "plot",
    "state",
    "state/baselines",
    "state/entities",
    "state/entities/characters",
    "state/entities/locations",
    "state/entities/items",
    "state/entities/quests",
    "state/entities/foreshadows",
    "state/entities/relationships",
    "state/events",
    "state/chapters",
    "state/deltas",
    "state/arc-summaries",
    "state/context",
    "state/reviews",
    "drafts",
    "chapters",
    "revisions",
    "audits",
    "exports",
)
MANAGED_FILES = (
    ".novel/transaction.json",
    ".novel/workspace.lock",
    "state/events/events.jsonl",
    "state/current.json",
    "state/writing-settings.json",
    "canon/style-reference.md",
    "state/creative-lessons.md",
    "state/session-handoff.md",
    "plot/current-arc.md",
)

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

CHARACTER_STATUSES = {"active", "inactive", "missing", "dead", "unknown"}
ITEM_STATUSES = {"available", "held", "lost", "consumed", "destroyed", "inactive", "unknown"}
QUEST_STATUSES = {"planned", "open", "active", "paused", "completed", "failed", "cancelled", "unknown"}
RELATIONSHIP_STATUSES = {"active", "inactive", "broken", "resolved", "unknown"}
LOCATION_STATUSES = {"active", "inactive", "destroyed", "sealed", "unknown"}
FORESHADOW_STATUSES = {"open", "touched", "resolved", "abandoned", "deferred", "unknown"}

EVENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "knowledge_gained": {"required": {"character_id": "CHAR", "fact_id": "text"}},
    "knowledge_lost": {"required": {"character_id": "CHAR", "fact_id": "text"}},
    "character_moved": {"required": {"character_id": "CHAR", "location_id": "LOC"}},
    "character_status_changed": {"required": {"character_id": "CHAR", "status": "text"}, "status_values": CHARACTER_STATUSES},
    "skill_gained": {"required": {"character_id": "CHAR", "skill": "text"}},
    "skill_lost": {"required": {"character_id": "CHAR", "skill": "text"}},
    "item_acquired": {"required": {"item_id": "ITEM", "owner": "entity"}},
    "item_transfer": {"required": {"item_id": "ITEM", "from": "entity", "to": "entity"}},
    "item_consumed": {"required": {"item_id": "ITEM"}},
    "item_destroyed": {"required": {"item_id": "ITEM"}},
    "item_lost": {"required": {"item_id": "ITEM"}},
    "quest_opened": {"required": {"quest_id": "QUEST"}},
    "quest_status_changed": {"required": {"quest_id": "QUEST", "status": "text"}, "status_values": QUEST_STATUSES},
    "relationship_changed": {
        "required": {"relationship_id": "REL"},
        "optional": {"status": "text", "stage": "text"},
        "one_of": ("status", "stage"),
        "status_values": RELATIONSHIP_STATUSES,
    },
    "location_status_changed": {"required": {"location_id": "LOC", "status": "text"}, "status_values": LOCATION_STATUSES},
    "foreshadow_status_changed": {"required": {"foreshadow_id": "FS", "status": "text"}, "status_values": FORESHADOW_STATUSES},
    "note": {"required": {"summary": "text"}, "requires_state_effect_false": True},
}


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def read_text(path: Path, *, required: bool = False) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise
        return ""


def load_json(path: Path, *, default: Any = None, required: bool = False) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        if required:
            raise
        return default


def load_events(path: Path, *, required: bool = False) -> list[dict[str, Any]]:
    if not path.exists():
        if required:
            raise FileNotFoundError(path)
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path} line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"event row must be an object: {path} line {line_number}")
            rows.append(row)
    return rows


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry update where the platform supports directory fsync."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting data")
        view = view[written:]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def ensure_within(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path is outside allowed directory: {resolved}") from exc
    return resolved


def safe_relative_path(value: str, *, field: str = "path") -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    if candidate.is_absolute() or candidate.anchor or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"{field} must not be absolute or contain traversal: {value!r}")
    for part in candidate.parts:
        if part.rstrip(" .") != part or ":" in part:
            raise ValueError(f"{field} contains a Windows-ambiguous path component: {part!r}")
        stem = part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"{field} contains a reserved device name: {part!r}")
    return candidate


def require_int(value: Any, field: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """Return a real integer or raise a field-specific validation error.

    bool is rejected because JSON booleans are Python integers. This helper is used at
    trust boundaries so malformed project data becomes a reportable error rather than
    an uncaught traceback.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return value


def _relative_under(path: Path, stop: Path) -> tuple[Path, Path]:
    """Return a stable root and relative path without Windows case/8.3 false positives.

    Prefer the lexical path so a symlink component remains visible to lstat. When the
    caller used a Windows short-name or differently-cased alias for the same directory,
    fall back to resolving both sides before deriving the relative path.
    """
    lexical_root = Path(os.path.abspath(stop))
    lexical_path = Path(os.path.abspath(path))
    try:
        return lexical_root, lexical_path.relative_to(lexical_root)
    except ValueError:
        resolved_root = stop.resolve(strict=True)
        resolved_path = path.resolve(strict=False)
        try:
            return resolved_root, resolved_path.relative_to(resolved_root)
        except ValueError:
            if os.name == "nt":
                root_text = os.path.normcase(str(resolved_root))
                path_text = os.path.normcase(str(resolved_path))
                try:
                    common = os.path.commonpath([root_text, path_text])
                except ValueError as exc:
                    raise ValueError(f"path escapes protected root: {path}") from exc
                if common != root_text:
                    raise ValueError(f"path escapes protected root: {path}")
                return resolved_root, Path(os.path.relpath(path_text, root_text))
            raise ValueError(f"path escapes protected root: {path}")


def ensure_no_symlink_chain(path: Path, stop: Path, *, allow_missing: bool = True) -> None:
    root, relative = _relative_under(path, stop)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_or_symlink(current):
                raise ValueError(f"link or reparse point is not allowed in protected path: {current}")
        elif not allow_missing:
            raise ValueError(f"required path is missing: {current}")
    resolved_root = stop.resolve(strict=True)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        if os.name != "nt":
            raise ValueError(f"path escapes protected root after resolution: {path}") from exc
        root_text = os.path.normcase(str(resolved_root))
        path_text = os.path.normcase(str(resolved_path))
        try:
            common = os.path.commonpath([root_text, path_text])
        except ValueError as inner:
            raise ValueError(f"path escapes protected root after resolution: {path}") from inner
        if common != root_text:
            raise ValueError(f"path escapes protected root after resolution: {path}")


def validate_workspace_layout(root: Path) -> Path:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"workspace is not a directory: {root}")
    for relative in MANAGED_DIRECTORIES + MANAGED_FILES:
        path = root / relative
        ensure_no_symlink_chain(path, root, allow_missing=True)
        if relative in MANAGED_FILES and path.exists() and path.is_file():
            info = path.stat()
            if getattr(info, "st_nlink", 1) != 1:
                raise ValueError(f"hard-linked managed file is not allowed: {path}")
    return root


def remove_transaction_backup(root: Path, backup_root: Path, *, ignore_missing: bool = True) -> bool:
    """Remove one validated transaction backup without following links or junctions.

    The exact backup directory is revalidated immediately before deletion. The helper
    refuses symlinks/reparse points and directories outside `.novel/backups`; callers
    may treat a cleanup failure as garbage-collection debt after the transaction marker
    has already been removed.
    """
    root = validate_workspace_layout(root)
    backup_base = safe_workspace_path(root, ".novel/backups", allow_missing=True)
    ensure_no_symlink_chain(backup_base, root, allow_missing=True)
    if not backup_root.exists() and not backup_root.is_symlink():
        if ignore_missing:
            return False
        raise FileNotFoundError(backup_root)
    # Re-check the full chain and the final entry immediately before rmtree.
    ensure_no_symlink_chain(backup_root, backup_base, allow_missing=False)
    if _is_reparse_or_symlink(backup_root):
        raise ValueError(f"transaction backup must not be a link or reparse point: {backup_root}")
    resolved_base = backup_base.resolve(strict=True)
    resolved_backup = backup_root.resolve(strict=True)
    try:
        relative = resolved_backup.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"transaction backup escapes backup root: {backup_root}") from exc
    if len(relative.parts) != 1 or not TRANSACTION_ID_RE.fullmatch(relative.name):
        raise ValueError(f"transaction backup path is not an exact transaction directory: {backup_root}")
    info = backup_root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"transaction backup must be a directory: {backup_root}")
    shutil.rmtree(backup_root, ignore_errors=False)
    _fsync_directory(resolved_base)
    return True


def safe_workspace_path(root: Path, relative: str | PurePosixPath, *, allow_missing: bool = True) -> Path:
    rel = relative if isinstance(relative, PurePosixPath) else safe_relative_path(relative)
    path = root.joinpath(*rel.parts)
    ensure_no_symlink_chain(path, root, allow_missing=allow_missing)
    return ensure_within(path, root)


def output_under(root: Path, subdir: str, name: str, default_name: str) -> Path:
    validate_workspace_layout(root)
    clean = Path(name or default_name).name
    if clean in {"", ".", ".."}:
        clean = default_name
    safe_relative_path(clean, field="output filename")
    base = safe_workspace_path(root, safe_relative_path(subdir, field="output directory"))
    target = base / clean
    ensure_no_symlink_chain(target, root, allow_missing=True)
    return ensure_within(target, base)


def append_jsonl_no_follow(path: Path, rows: list[dict[str, Any]], root: Path) -> None:
    validate_workspace_layout(root)
    ensure_no_symlink_chain(path, root, allow_missing=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"event log must be a regular file: {path}")
        if getattr(info, "st_nlink", 1) != 1:
            raise ValueError(f"hard-linked event log is not allowed: {path}")
        payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")
        if payload:
            _write_all(fd, payload)
            os.fsync(fd)
    finally:
        os.close(fd)


def file_prefix_sha256(path: Path, size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    with path.open("rb") as handle:
        while remaining:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"file is shorter than required prefix: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def truncate_no_follow(path: Path, size: int, root: Path, *, prefix_sha256: str = "") -> None:
    validate_workspace_layout(root)
    ensure_no_symlink_chain(path, root, allow_missing=False)
    current_size = path.stat().st_size
    if size < 0 or size > current_size:
        raise ValueError(f"rollback size must not expand event log: requested={size}, current={current_size}")
    if prefix_sha256 and file_prefix_sha256(path, size) != prefix_sha256:
        raise ValueError("event log prefix changed; refusing destructive rollback")
    flags = os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"event log must be a regular file: {path}")
        if getattr(info, "st_nlink", 1) != 1:
            raise ValueError(f"hard-linked event log is not allowed: {path}")
        os.ftruncate(fd, size)
        os.fsync(fd)
    finally:
        os.close(fd)


def chapter_filename(number: int) -> str:
    width = max(4, len(str(number)))
    return f"chapter-{number:0{width}d}.md"


def chapter_meta_filename(number: int) -> str:
    width = max(4, len(str(number)))
    return f"chapter-{number:0{width}d}.json"


def list_chapters(root: Path) -> list[tuple[int, Path]]:
    result: list[tuple[int, Path]] = []
    chapter_dir = root / "chapters"
    if not chapter_dir.exists():
        return result
    ensure_no_symlink_chain(chapter_dir, root, allow_missing=False)
    for path in chapter_dir.glob("chapter-*.md"):
        ensure_no_symlink_chain(path, root, allow_missing=False)
        match = CHAPTER_FILE_RE.fullmatch(path.name)
        if match:
            result.append((int(match.group(1)), path))
    return sorted(result)


def chinese_number(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = section = number = 0
    for char in text:
        if char in CN_DIGITS:
            number = CN_DIGITS[char]
        elif char in CN_UNITS:
            unit = CN_UNITS[char]
            if unit == 10000:
                section = (section + number) * unit
                total += section
                section = number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            return None
    return total + section + number


def first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return re.sub(r"^#+\s*", "", stripped).strip()
        if stripped:
            return ""
    return ""


def title_chapter_number(text: str) -> int | None:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    match = re.match(r"^#\s*第\s*([零〇一二两三四五六七八九十百千万\d]+)\s*章(?:\s|$)", first)
    if match:
        return chinese_number(match.group(1))
    match = re.match(r"^#\s*Chapter\s+(\d+)(?:\s|$)", first, re.I)
    return int(match.group(1)) if match else None


def normalize_entity_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if not ENTITY_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid entity id: {value!r}")
    return normalized


def normalize_event_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if not EVENT_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid event id: {value!r}")
    return normalized


def event_id_parts(value: str) -> tuple[int, int]:
    normalized = normalize_event_id(value)
    match = EVENT_ID_RE.fullmatch(normalized)
    assert match
    return int(match.group(1)), int(match.group(2))


def normalize_arc_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if not ARC_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid arc id: {value!r}")
    return normalized


def normalize_node_id(value: str) -> str:
    normalized = str(value).strip().upper()
    if not NODE_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid outline node id: {value!r}")
    return normalized


def extract_entity_ids(text: str) -> set[str]:
    return {match.group(0).upper() for match in ENTITY_ID_RE.finditer(text)}


def event_entity_ids(event: dict[str, Any]) -> set[str]:
    values: list[Any] = list(event.get("entities", [])) if isinstance(event.get("entities", []), list) else []
    for field in (
        "item_id", "owner", "from", "to", "character_id", "location_id", "quest_id",
        "foreshadow_id", "relationship_id", "subject_id", "target_id",
    ):
        value = event.get(field)
        if isinstance(value, str) and value:
            values.append(value)
    ids: set[str] = set()
    for value in values:
        ids.update(extract_entity_ids(str(value)))
    return ids


def entity_path(root: Path, entity_id: str) -> Path:
    entity_id = normalize_entity_id(entity_id)
    prefix = entity_id.split("-", 1)[0]
    directory = ENTITY_PREFIX_TO_DIR.get(prefix)
    if not directory:
        raise ValueError(f"unknown entity id prefix: {entity_id}")
    path = root / "state" / "entities" / directory / f"{entity_id}.json"
    ensure_no_symlink_chain(path, root, allow_missing=True)
    return path


def baseline_path(root: Path, entity_id: str) -> Path:
    entity_id = normalize_entity_id(entity_id)
    path = root / "state" / "baselines" / f"{entity_id}.json"
    ensure_no_symlink_chain(path, root, allow_missing=True)
    return path


def arc_summary_path(root: Path, arc_id: str) -> Path | None:
    normalized = normalize_arc_id(arc_id)
    directory = root / "state" / "arc-summaries"
    ensure_no_symlink_chain(directory, root, allow_missing=True)
    matches = []
    if directory.exists():
        for path in directory.glob("*.md"):
            ensure_no_symlink_chain(path, root, allow_missing=False)
            if path.stem.upper() == normalized:
                matches.append(path)
    if len(matches) > 1:
        raise ValueError(f"duplicate arc summary id: {normalized}")
    return matches[0] if matches else None


def heading_ids(line: str, pattern: re.Pattern[str]) -> set[str]:
    if not re.match(r"^#{1,6}\s+", line):
        return set()
    return {match.group(0).upper() for match in pattern.finditer(line)}


def markdown_intro_and_exact_section(text: str, marker: str, pattern: re.Pattern[str]) -> str | None:
    lines = text.splitlines()
    heading_positions = [index for index, line in enumerate(lines) if re.match(r"^#{1,6}\s+", line)]
    if not heading_positions:
        return None
    intro = "\n".join(lines[: heading_positions[0]]).strip()
    normalized = marker.upper()
    for pos_index, start in enumerate(heading_positions):
        if normalized not in heading_ids(lines[start], pattern):
            continue
        level = len(lines[start]) - len(lines[start].lstrip("#"))
        end = len(lines)
        for later in heading_positions[pos_index + 1 :]:
            later_level = len(lines[later]) - len(lines[later].lstrip("#"))
            if later_level <= level:
                end = later
                break
        block = "\n".join(lines[start:end]).strip()
        return "\n\n".join(part for part in (intro, block) if part)
    return None


def deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def immutable_fields_for(entity_id: str) -> set[str]:
    prefix = normalize_entity_id(entity_id).split("-", 1)[0]
    return set(IMMUTABLE_BASELINE_FIELDS.get(prefix, {"created_chapter"}))


def build_baseline_record(entity: dict[str, Any]) -> dict[str, Any]:
    entity_id = normalize_entity_id(str(entity.get("id", "")))
    fields = sorted(immutable_fields_for(entity_id))
    values = {field: entity.get(field) for field in fields}
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schema": 1,
        "entity_id": entity_id,
        "created_chapter": require_int(entity.get("created_chapter"), "entity.created_chapter", minimum=1),
        "fields": values,
        "fields_sha256": sha256_text(payload),
    }


def validate_baseline_record(record: Any, entity: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict) or record.get("schema") != 1 or isinstance(record.get("schema"), bool):
        return ["baseline record must be a schema 1 object"]
    try:
        entity_id = normalize_entity_id(str(entity.get("id", "")))
    except ValueError as exc:
        return [str(exc)]
    if str(record.get("entity_id", "")).upper() != entity_id:
        errors.append("baseline entity_id mismatch")
    try:
        expected = build_baseline_record(entity)
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    if record.get("fields") != expected["fields"]:
        errors.append("entity immutable baseline differs from sealed baseline")
    if str(record.get("fields_sha256", "")) != expected["fields_sha256"]:
        errors.append("baseline hash mismatch")
    record_chapter = record.get("created_chapter")
    entity_chapter = entity.get("created_chapter")
    if not isinstance(record_chapter, int) or isinstance(record_chapter, bool) or record_chapter != entity_chapter:
        errors.append("baseline created_chapter mismatch")
    return errors


def split_markdown_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the simple scalar YAML frontmatter used by project handoff files."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    for raw in text[4:end].splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if value.isdigit():
            meta[key] = int(value)
        elif value.casefold() in {"true", "false"}:
            meta[key] = value.casefold() == "true"
        else:
            meta[key] = value
    return meta, text[end + 5 :]


def token_set(text: str) -> set[str]:
    lowered = text.casefold()
    tokens = set(re.findall(r"[a-z0-9_-]{2,}", lowered))
    chinese = "".join(re.findall(r"[\u3400-\u9fff]", lowered))
    tokens.update(chinese[i : i + 2] for i in range(max(0, len(chinese) - 1)))
    tokens.update(extract_entity_ids(text))
    return {token for token in tokens if token}



SCENE_BRIDGE_FIELDS = (
    "time",
    "location",
    "pov",
    "last_action",
    "immediate_pressure",
    "emotional_residue",
)

CHAPTER_FUNCTIONS = {"", "advance", "deepen", "build", "mixed"}
READER_REVIEW_REASONS = {"batch", "milestone", "manual", "periodic", "first-chapter"}
READER_REVIEW_VERDICTS = {"strong", "acceptable", "weak"}
READER_ENDING_PULLS = {"strong", "fair", "weak"}
MAX_READER_ISSUE_TAGS = 3


def validate_scene_bridge(value: Any, *, field: str = "scene_bridge") -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    errors: list[str] = []
    for key in SCENE_BRIDGE_FIELDS:
        if key in value and not isinstance(value.get(key), str):
            errors.append(f"{field}.{key} must be a string")
    return errors


def validate_reader_review(
    value: Any,
    *,
    field: str = "reader_review",
    expected_chapter: int | None = None,
    expected_batch: dict[str, int] | None = None,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{field} must be an object"]
    errors: list[str] = []
    required = ("reviewed_through_chapter", "reason", "verdict", "ending_pull", "revision_applied")
    for key in required:
        if key not in value:
            errors.append(f"{field}.{key} is required")
    chapter = value.get("reviewed_through_chapter")
    if not isinstance(chapter, int) or isinstance(chapter, bool) or chapter < 0:
        errors.append(f"{field}.reviewed_through_chapter must be a non-negative integer")
    elif expected_chapter is not None and chapter != expected_chapter:
        errors.append(f"{field}.reviewed_through_chapter must equal {expected_chapter}")
    reason = value.get("reason")
    if not isinstance(reason, str) or reason not in READER_REVIEW_REASONS:
        errors.append(f"{field}.reason must be one of {sorted(READER_REVIEW_REASONS)}")
    verdict = value.get("verdict")
    if not isinstance(verdict, str) or verdict not in READER_REVIEW_VERDICTS:
        errors.append(f"{field}.verdict must be one of {sorted(READER_REVIEW_VERDICTS)}")
    pull = value.get("ending_pull")
    if not isinstance(pull, str) or pull not in READER_ENDING_PULLS:
        errors.append(f"{field}.ending_pull must be one of {sorted(READER_ENDING_PULLS)}")
    if not isinstance(value.get("revision_applied"), bool):
        errors.append(f"{field}.revision_applied must be a boolean")
    issue_tags = value.get("issue_tags", [])
    if not isinstance(issue_tags, list):
        errors.append(f"{field}.issue_tags must be a list")
    else:
        if len(issue_tags) > MAX_READER_ISSUE_TAGS:
            errors.append(f"{field}.issue_tags must contain at most {MAX_READER_ISSUE_TAGS} items")
        for index, tag in enumerate(issue_tags):
            if not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 80:
                errors.append(f"{field}.issue_tags[{index}] must be a non-empty string of at most 80 characters")
    if "highest_value_revision" in value and not isinstance(value.get("highest_value_revision"), str):
        errors.append(f"{field}.highest_value_revision must be a string")
    if reason == "batch":
        batch_keys = ("batch_id", "batch_start_chapter", "batch_end_chapter")
        # Older projects may contain a pre-anchor batch review. New commits pass an
        # expected batch and therefore require all anchor fields; legacy audits remain readable.
        anchored = expected_batch is not None or any(key in value for key in batch_keys)
        if anchored:
            for key in batch_keys:
                raw = value.get(key)
                if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
                    errors.append(f"{field}.{key} must be a positive integer for batch reviews")
            if isinstance(value.get("batch_end_chapter"), int) and chapter != value.get("batch_end_chapter"):
                errors.append(f"{field}.reviewed_through_chapter must equal batch_end_chapter")
        if expected_batch is not None:
            expected = {
                "batch_id": expected_batch["batch_id"],
                "batch_start_chapter": expected_batch["start_chapter"],
                "batch_end_chapter": expected_batch["end_chapter"],
            }
            for key, expected_value in expected.items():
                if value.get(key) != expected_value:
                    errors.append(f"{field}.{key} must equal {expected_value}")
    return errors


def validate_chapter_function_fields(data: Any, *, field: str = "chapter") -> list[str]:
    if not isinstance(data, dict):
        return [f"{field} must be an object"]
    errors: list[str] = []
    function = data.get("chapter_function", "")
    if not isinstance(function, str) or function not in CHAPTER_FUNCTIONS:
        errors.append(f"{field}.chapter_function must be one of {sorted(CHAPTER_FUNCTIONS)}")
    for key in ("dominant_change", "reader_expectation_added"):
        if key in data and not isinstance(data.get(key), str):
            errors.append(f"{field}.{key} must be a string")
    return errors


def validate_current_entity_lists(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        return [f"{field} must be a list"]
    errors: list[str] = []
    for item in value:
        try:
            normalize_entity_id(str(item))
        except ValueError as exc:
            errors.append(f"{field}: {exc}")
    return errors


def relevance_score(query: str, text: str) -> float:
    query_ids = extract_entity_ids(query)
    text_ids = extract_entity_ids(text)
    score = 30.0 * len(query_ids & text_ids)
    q = token_set(query)
    if not q:
        return score
    t = token_set(text)
    overlap = len(q & t)
    score += overlap * 2.0
    score += overlap / max(1, len(q))
    return score


def validate_entity_data(data: Any, expected_id: str | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["entity must be a JSON object"]
    try:
        entity_id = normalize_entity_id(str(data.get("id", "")))
    except ValueError as exc:
        errors.append(str(exc))
        entity_id = ""
    if expected_id and entity_id and entity_id != expected_id:
        errors.append(f"entity id mismatch: expected {expected_id}, got {entity_id}")
    schema = data.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != 1:
        errors.append("entity schema must be integer 1")
    if not str(data.get("name", "")).strip():
        errors.append("entity name is required")
    if not str(data.get("status", "")).strip():
        errors.append("entity status is required")
    created_chapter = data.get("created_chapter")
    if not isinstance(created_chapter, int) or isinstance(created_chapter, bool):
        errors.append("created_chapter must be an integer")
    elif created_chapter < 1:
        errors.append("created_chapter must be a positive integer")
    if entity_id.startswith("CHAR-"):
        for field in ("aliases", "initial_knowledge", "initial_skills", "knowledge", "skills"):
            if field not in data:
                errors.append(f"character {field} is required")
            elif not isinstance(data.get(field), list):
                errors.append(f"character {field} must be a list")
        for field in ("initial_status", "initial_location"):
            if field not in data:
                errors.append(f"character {field} is required")
        for field in ("personal_game", "current_stake", "rule_view", "non_optimal_choice_pattern"):
            if field in data and not isinstance(data.get(field), str):
                errors.append(f"character {field} must be a string")
        voice = data.get("voice", "")
        if not isinstance(voice, (str, dict)):
            errors.append("character voice must be a string or object")
        elif isinstance(voice, dict):
            text_fields = (
                "sentence_shape", "lexicon", "speaking_tempo", "preferred_metaphors",
                "emotion_leaks_through", "what_the_character_notices_first",
                "pressure_response", "conclusion_style", "explains_decisions",
                "sarcasm_target", "actions_instead_of_emotion",
            )
            list_fields = (
                "avoided_topics", "verbal_tics", "voluntary_topics", "words_never_used",
                "voice_examples", "voice_anti_examples",
            )
            for field in text_fields:
                if field in voice and not isinstance(voice.get(field), str):
                    errors.append(f"character voice.{field} must be a string")
            for field in list_fields:
                if field in voice and (not isinstance(voice.get(field), list) or not all(isinstance(item, str) for item in voice.get(field, []))):
                    errors.append(f"character voice.{field} must be a list of strings")
                elif field in {"voice_examples", "voice_anti_examples"} and len(voice.get(field, [])) > 3:
                    errors.append(f"character voice.{field} must contain at most 3 short examples")
    elif entity_id.startswith("ITEM-"):
        for field in ("initial_status", "initial_owner"):
            if field not in data:
                errors.append(f"item {field} is required")
    elif entity_id.startswith("REL-"):
        for field in ("initial_status", "initial_stage"):
            if field not in data:
                errors.append(f"relationship {field} is required")
    elif entity_id.startswith(("QUEST-", "LOC-", "FS-")) and "initial_status" not in data:
        errors.append("initial_status is required")
    return errors


def normalize_knowledge_used(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        raise ValueError("knowledge_used must be an object")
    result: dict[str, list[dict[str, Any]]] = {}
    for raw_character, raw_facts in value.items():
        character_id = normalize_entity_id(str(raw_character))
        if not character_id.startswith("CHAR-"):
            raise ValueError(f"knowledge_used key must be a character id: {character_id}")
        if not isinstance(raw_facts, list):
            raise ValueError(f"knowledge_used[{character_id}] must be a list")
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for raw in raw_facts:
            if isinstance(raw, str):
                fact_id, sequence = raw.strip(), 0
            elif isinstance(raw, dict):
                fact_id = str(raw.get("fact_id", "")).strip()
                sequence = raw.get("sequence", 0)
            else:
                raise ValueError(f"knowledge_used[{character_id}] entries must be strings or objects")
            if not fact_id:
                raise ValueError(f"knowledge_used[{character_id}] fact_id is required")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                raise ValueError(f"knowledge_used[{character_id}] sequence must be a non-negative integer")
            key = (fact_id, sequence)
            if key in seen:
                raise ValueError(f"duplicate knowledge use: {character_id} {fact_id} at {sequence}")
            seen.add(key)
            items.append({"fact_id": fact_id, "sequence": sequence})
        result[character_id] = items
    return result


def normalize_state_used(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("state_used must be a list")
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("state_used entries must be objects")
        entity_id = normalize_entity_id(str(raw.get("entity_id", "")))
        field = str(raw.get("field", "")).strip()
        sequence = raw.get("sequence", 0)
        if not field:
            raise ValueError("state_used.field is required")
        if "equals" not in raw:
            raise ValueError("state_used.equals is required")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise ValueError("state_used.sequence must be a non-negative integer")
        result.append({"entity_id": entity_id, "field": field, "equals": raw.get("equals"), "sequence": sequence})
    return result


def _validate_typed_value(field: str, value: Any, kind: str, known_entities: set[str]) -> list[str]:
    errors: list[str] = []
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} must be a non-empty string")
        return errors
    try:
        entity_id = normalize_entity_id(str(value))
    except ValueError as exc:
        return [str(exc)]
    if kind != "entity" and not entity_id.startswith(kind + "-"):
        errors.append(f"{field} must use a {kind}- id")
    if entity_id not in known_entities:
        errors.append(f"{field} references missing entity: {entity_id}")
    return errors


def validate_event_payload(row: Any, chapter: int, known_entities: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(row, dict):
        return ["event must be an object"]
    try:
        event_id = normalize_event_id(str(row.get("id", "")))
        id_chapter, id_sequence = event_id_parts(event_id)
        if id_chapter != chapter:
            errors.append(f"event id chapter must equal {chapter}: {event_id}")
        if isinstance(row.get("sequence"), int) and not isinstance(row.get("sequence"), bool) and id_sequence != row.get("sequence"):
            errors.append(f"event id sequence must equal sequence: {event_id}")
    except ValueError as exc:
        errors.append(str(exc))
    row_chapter = row.get("chapter")
    if not isinstance(row_chapter, int) or isinstance(row_chapter, bool) or row_chapter != chapter:
        errors.append("event chapter does not match the chapter being committed")
    sequence = row.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        errors.append("event sequence must be a positive integer")
    event_type = str(row.get("type", "")).strip()
    schema = EVENT_SCHEMAS.get(event_type)
    if schema is None:
        errors.append(f"unsupported event type: {event_type or '<empty>'}")
        return errors
    for field, kind in schema.get("required", {}).items():
        if field not in row:
            errors.append(f"event {event_type} requires {field}")
            continue
        errors.extend(_validate_typed_value(field, row.get(field), kind, known_entities))
    for field, kind in schema.get("optional", {}).items():
        if field in row and row.get(field) is not None:
            errors.extend(_validate_typed_value(field, row.get(field), kind, known_entities))
    related = row.get("entities", [])
    if "entities" in row and not isinstance(related, list):
        errors.append("event entities must be a list")
    elif isinstance(related, list):
        for value in related:
            try:
                entity_id = normalize_entity_id(str(value))
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if entity_id not in known_entities:
                errors.append(f"event entities references missing entity: {entity_id}")
    one_of = schema.get("one_of")
    if one_of and not any(str(row.get(field, "")).strip() for field in one_of):
        errors.append(f"event {event_type} requires one of: {', '.join(one_of)}")
    if "status" in row and schema.get("status_values") and row.get("status") not in schema["status_values"]:
        errors.append(f"event {event_type} has invalid status: {row.get('status')!r}")
    if schema.get("requires_state_effect_false") and row.get("state_effect") is not False:
        errors.append("note events must set state_effect=false")
    return errors


def validate_event_collection(events: Any, chapter: int, known_entities: set[str]) -> tuple[list[dict[str, Any]], list[str]]:
    if not isinstance(events, list):
        return [], ["events must be a list"]
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    ids: set[str] = set()
    sequences: set[int] = set()
    for index, raw in enumerate(events, 1):
        if not isinstance(raw, dict):
            errors.append("events entries must be objects")
            continue
        row = dict(raw)
        row.setdefault("chapter", chapter)
        row.setdefault("sequence", index)
        sequence_for_id = row.get("sequence") if isinstance(row.get("sequence"), int) and not isinstance(row.get("sequence"), bool) and row.get("sequence") > 0 else index
        row.setdefault("id", f"EVT-{chapter:04d}-{sequence_for_id:03d}")
        row["id"] = str(row["id"]).upper()
        event_errors = validate_event_payload(row, chapter, known_entities)
        errors.extend(event_errors)
        if isinstance(row.get("sequence"), int) and not isinstance(row.get("sequence"), bool):
            if row["sequence"] in sequences:
                errors.append(f"duplicate event sequence in chapter {chapter}: {row['sequence']}")
            sequences.add(row["sequence"])
        if row["id"] in ids:
            errors.append(f"duplicate event id: {row['id']}")
        ids.add(row["id"])
        normalized.append(row)
    return normalized, errors


def allowed_transaction_target(relative: PurePosixPath) -> bool:
    value = relative.as_posix()
    patterns = (
        r"state/current\.json",
        r"plot/current-arc\.md",
        r"chapters/chapter-\d+\.md",
        r"state/chapters/chapter-\d+\.json",
        r"state/deltas/chapter-\d+\.json",
        r"revisions/chapter-\d+/revision-\d+\.(?:md|json)",
        r"state/baselines/(?:CHAR|LOC|ITEM|QUEST|FS|REL)-\d+\.json",
        r"state/entities/(?:characters|locations|items|quests|foreshadows|relationships)/(?:CHAR|LOC|ITEM|QUEST|FS|REL)-\d+\.json",
    )
    return any(re.fullmatch(pattern, value, re.I) for pattern in patterns)


def validate_transaction_journal(root: Path, journal: Any, *, require_backups: bool | None = None) -> tuple[Path, list[dict[str, Any]]]:
    validate_workspace_layout(root)
    if not isinstance(journal, dict):
        raise ValueError("transaction journal must be an object")
    if journal.get("schema") != 1 or isinstance(journal.get("schema"), bool):
        raise ValueError("transaction journal schema must be integer 1")
    status_value = str(journal.get("status", ""))
    if status_value not in {"applying", "dirty", "committed", "cleanup_pending"}:
        raise ValueError("invalid transaction status")
    if require_backups is None:
        require_backups = status_value in {"applying", "dirty"}
    transaction_id = str(journal.get("transaction_id", ""))
    if not TRANSACTION_ID_RE.fullmatch(transaction_id):
        raise ValueError("invalid transaction_id")
    expected_backup = PurePosixPath(".novel") / "backups" / transaction_id
    actual_backup = safe_relative_path(str(journal.get("backup_dir", "")), field="backup_dir")
    if actual_backup != expected_backup:
        raise ValueError("backup_dir does not match transaction_id")
    backup_root = safe_workspace_path(root, actual_backup, allow_missing=not require_backups)
    records = journal.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("transaction journal files must be a non-empty list")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("transaction file record must be an object")
        relative = safe_relative_path(str(record.get("path", "")), field="files.path")
        if not allowed_transaction_target(relative):
            raise ValueError(f"transaction target is not allowed: {relative.as_posix()}")
        if relative.as_posix() in seen:
            raise ValueError(f"duplicate transaction target: {relative.as_posix()}")
        seen.add(relative.as_posix())
        target = safe_workspace_path(root, relative, allow_missing=True)
        source = backup_root.joinpath(*relative.parts)
        if backup_root.exists():
            ensure_no_symlink_chain(source, backup_root, allow_missing=True)
        existed = record.get("existed")
        if not isinstance(existed, bool):
            raise ValueError(f"files.existed must be boolean: {relative.as_posix()}")
        if require_backups and existed and not source.is_file():
            raise ValueError(f"required transaction backup is missing: {relative.as_posix()}")
        validated.append({"path": relative.as_posix(), "existed": existed, "target": target, "source": source})
    events_size = journal.get("events_size", 0)
    if not isinstance(events_size, int) or isinstance(events_size, bool) or events_size < 0:
        raise ValueError("events_size must be a non-negative integer")
    if require_backups:
        events_path = safe_workspace_path(root, "state/events/events.jsonl", allow_missing=False)
        current_size = events_path.stat().st_size
        if events_size > current_size:
            raise ValueError("events_size must not exceed the current event log size")
        prefix = str(journal.get("events_prefix_sha256", ""))
        if prefix and file_prefix_sha256(events_path, events_size) != prefix:
            raise ValueError("event log prefix does not match transaction journal")
    return backup_root, validated


@contextlib.contextmanager
def workspace_lock(root: Path, timeout: float = 30.0) -> Iterator[None]:
    root = root.resolve(strict=True)
    validate_workspace_layout(root)
    lock_dir = root / ".novel"
    lock_dir.mkdir(parents=True, exist_ok=True)
    ensure_no_symlink_chain(lock_dir, root, allow_missing=False)
    lock_path = lock_dir / "workspace.lock"
    ensure_no_symlink_chain(lock_path, root, allow_missing=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_nlink", 1) != 1:
        os.close(fd)
        raise ValueError(f"workspace lock must be a non-linked regular file: {lock_path}")
    handle = os.fdopen(fd, "r+b", buffering=0)
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("workspace is busy; retry after the other chapter operation finishes")
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def validate_revision_history(root: Path, chapter: int, meta: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(meta, dict):
        return [f"chapter {chapter} metadata must be an object"]
    history = meta.get("revision_history", [])
    if not isinstance(history, list):
        return [f"chapter {chapter} revision_history must be a list"]
    revision = meta.get("revision", 1)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return [f"chapter {chapter} revision must be a positive integer"]
    expected = list(range(1, revision))
    actual: list[int] = []
    revision_root = root / "revisions"
    for record in history:
        if not isinstance(record, dict):
            errors.append(f"chapter {chapter} revision history entry must be an object")
            continue
        number = record.get("revision")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            errors.append(f"chapter {chapter} revision history number is invalid")
            continue
        actual.append(number)
        for path_field, hash_field in (("archive_prose", "prose_sha256"), ("archive_metadata", "metadata_sha256")):
            try:
                relative = safe_relative_path(str(record.get(path_field, "")), field=path_field)
                path = safe_workspace_path(root, relative, allow_missing=False)
                path.resolve().relative_to(revision_root.resolve())
            except (ValueError, FileNotFoundError) as exc:
                errors.append(f"chapter {chapter} revision {number} archive path is invalid: {exc}")
                continue
            if not path.is_file():
                errors.append(f"chapter {chapter} revision {number} archive is missing: {path_field}")
                continue
            if sha256_bytes(path.read_bytes()) != str(record.get(hash_field, "")):
                errors.append(f"chapter {chapter} revision {number} archive hash mismatch: {path_field}")
    if sorted(actual) != expected:
        errors.append(f"chapter {chapter} revision chain is incomplete: expected {expected}, found {sorted(actual)}")
    return errors
