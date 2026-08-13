#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from batch_review import chapter_key, current_prose_hash
from batch_state import load_active_review_unit
from common import load_json, read_text, safe_workspace_path, validate_workspace_layout

CN_NUM = r"[0-9零〇一二三四五六七八九十百千万两]+"
HARD_PATTERNS = [
    ("internal_path", re.compile(r"(?:\.novel/|\.claude/|state/current\.json\b|chapter-\d{4}\.md\b|/blind-packets/|/production/run-)")),
    ("placeholder", re.compile(r"(?i)(?:\{\{[^{}]{1,120}\}\}|\bTODO\b|\bTBD\b|<PLACEHOLDER>|\[PLACEHOLDER\])")),
    ("source_marker", re.compile(r"(?i)<!--\s*SOURCE\s*:|BEGIN[_ -]?(?:INTERNAL|PROMPT)|END[_ -]?(?:INTERNAL|PROMPT)")),
]
SEMANTIC_PATTERNS = [
    ("chapter_reference", re.compile(rf"(?:第{CN_NUM}章|前{CN_NUM}章|上一章|下一章|本章)")),
    ("volume_reference", re.compile(rf"(?:第{CN_NUM}卷|上一卷|下一卷|本卷|卷尾|卷首)")),
    ("production_term", re.compile(r"(?i)\b(?:prompt|writer|reader|reviewer|review|outline|story\s*bible|task\s*card|context\s*packet|batch|staging|canonical|schema|carousel|harmonization|subagent|agent)\b|(?:创作圣经|故事圣经|任务卡|写手任务卡|章节逻辑|章节节奏|卷尾钩子|爽文循环)")),
]


def _line_for(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _evidence(text: str, start: int, end: int, radius: int = 48) -> str:
    a=max(0,start-radius); b=min(len(text),end+radius)
    return re.sub(r"\s+", " ", text[a:b]).strip()[:160]

def _warning_id(chapter:int, line:int, category:str, evidence:str)->str:
    raw=f"{chapter}|{line}|{category}|{evidence}".encode("utf-8")
    return "W-"+hashlib.sha256(raw).hexdigest()[:16]


def scan_text(text: str, chapter: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blockers=[]; warnings=[]; blocked_spans=[]
    scan_source=re.sub(r"(?m)^#{1,6}[^\n]*$", lambda m: " " * len(m.group(0)), text)
    for category, pattern in HARD_PATTERNS:
        for m in pattern.finditer(scan_source):
            blocked_spans.append((m.start(),m.end()))
            blockers.append({"chapter":chapter,"line":_line_for(text,m.start()),"category":category,"evidence":_evidence(text,m.start(),m.end())})

    # Exact repeated prose paragraphs are deterministic enough to block; short repeats are not.
    para_seen: dict[str, int]={}; pos=0
    for raw in re.split(r"\n\s*\n", text):
        stripped=raw.strip(); idx=text.find(raw,pos); pos=max(pos,idx+len(raw)); norm=re.sub(r"\s+"," ",stripped)
        if len(norm)>=100 and not norm.startswith('#'):
            if norm in para_seen:
                blockers.append({"chapter":chapter,"line":_line_for(text,max(0,idx)),"category":"duplicate","evidence":norm[:160],"first_seen_line":para_seen[norm]})
            else:
                para_seen[norm]=_line_for(text,max(0,idx))

    seen_warning_ids=set()
    for category, pattern in SEMANTIC_PATTERNS:
        for m in pattern.finditer(scan_source):
            if any(not (m.end()<=a or m.start()>=b) for a,b in blocked_spans):
                continue
            line=_line_for(text,m.start()); evidence=_evidence(text,m.start(),m.end()); wid=_warning_id(chapter,line,category,evidence)
            if wid in seen_warning_ids:
                continue
            seen_warning_ids.add(wid)
            warnings.append({"id":wid,"chapter":chapter,"line":line,"category":category,"evidence":evidence})
    return blockers,warnings


def scan_review_unit(root: Path, unit: dict[str, Any]) -> dict[str, Any]:
    blocking=[]; warnings=[]; hashes={}
    for chapter in range(unit['start_chapter'],unit['end_chapter']+1):
        formal=safe_workspace_path(root,f"chapters/chapter-{chapter:04d}.md",allow_missing=True)
        staging=safe_workspace_path(root,f".novel/staging/chapter-{chapter:04d}.md",allow_missing=True)
        path=formal if formal.is_file() else staging
        if not path.is_file():
            blocking.append({"chapter":chapter,"line":0,"category":"missing","evidence":"chapter prose missing"}); continue
        text=read_text(path,required=True); b,w=scan_text(text,chapter); blocking.extend(b); warnings.extend(w)
        hashes[chapter_key(chapter)]=current_prose_hash(root,chapter)
    return {"status":"clean" if not blocking else "blocked","blocking":blocking,"warnings":warnings,"checked_hashes":hashes}


def main()->int:
    parser=argparse.ArgumentParser(description="Cheap deterministic final prose-cleanliness scan. Semantic candidates are warnings, not genre-blind blockers.")
    parser.add_argument('workspace',nargs='?',default='.')
    args=parser.parse_args(); root=Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/'state/current.json',required=True)
        if not isinstance(current,dict): raise ValueError('state/current.json must be an object')
        unit=load_active_review_unit(root,current); result=scan_review_unit(root,unit)
    except ValueError as exc: parser.error(str(exc))
    print(json.dumps(result,ensure_ascii=False)); return 0 if result['status']=='clean' else 1

if __name__=='__main__': raise SystemExit(main())
