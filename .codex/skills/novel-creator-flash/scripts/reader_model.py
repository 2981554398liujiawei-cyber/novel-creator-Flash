#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from common import load_json, safe_workspace_path, validate_workspace_layout

KINDS = {"promise", "question", "belief", "suspicion"}
ACTIONS = {
    "promise": {"introduced", "reinforced", "complicated", "partially_paid", "paid", "abandoned"},
    "question": {"opened", "reinforced", "answered", "abandoned"},
    "belief": {"introduced", "reinforced", "revised", "disproved"},
    "suspicion": {"introduced", "reinforced", "weakened", "resolved"},
}
LIST_KEYS = {"promise":"promises", "question":"open_questions", "belief":"beliefs", "suspicion":"suspicions"}
ACTIVE_TERMINAL = {
    "promise": {"paid", "abandoned"},
    "question": {"answered", "abandoned"},
    "belief": {"disproved"},
    "suspicion": {"resolved"},
}


def blank_model() -> dict[str, Any]:
    return {"schema":1,"promises":[],"open_questions":[],"beliefs":[],"suspicions":[],"updated_through_chapter":0}


def load_reader_model(root: Path) -> dict[str, Any]:
    path=safe_workspace_path(root,"state/reader-model.json",allow_missing=True)
    if not path.is_file(): return blank_model()
    data=load_json(path,required=True)
    if not isinstance(data,dict): raise ValueError("state/reader-model.json must be an object")
    errors=validate_model(data)
    if errors: raise ValueError("; ".join(errors))
    return data


def validate_model(data: Any) -> list[str]:
    if not isinstance(data, dict) or data.get("schema") != 1:
        return ["reader model must be a schema 1 object"]
    errors: list[str] = []
    for key in ("promises","open_questions","beliefs","suspicions"):
        rows=data.get(key)
        if not isinstance(rows,list): errors.append(f"reader model {key} must be a list"); continue
        seen=set()
        for i,row in enumerate(rows):
            if not isinstance(row,dict): errors.append(f"reader model {key}[{i}] must be an object"); continue
            rid=row.get("id")
            if not isinstance(rid,str) or not rid.strip(): errors.append(f"reader model {key}[{i}].id must be a string")
            elif rid in seen: errors.append(f"reader model {key} duplicates id {rid}")
            seen.add(rid)
            if not isinstance(row.get("text",""),str): errors.append(f"reader model {key}[{i}].text must be a string")
    u=data.get("updated_through_chapter",0)
    if not isinstance(u,int) or isinstance(u,bool) or u<0: errors.append("reader model updated_through_chapter must be a non-negative integer")
    return errors


def validate_updates(value: Any) -> list[str]:
    if value is None: return []
    if not isinstance(value,list): return ["reader_model_updates must be a list"]
    errors=[]
    for i,row in enumerate(value):
        if not isinstance(row,dict): errors.append(f"reader_model_updates[{i}] must be an object"); continue
        kind=row.get("kind"); action=row.get("action"); rid=row.get("id")
        if kind not in KINDS: errors.append(f"reader_model_updates[{i}].kind invalid")
        elif action not in ACTIONS[kind]: errors.append(f"reader_model_updates[{i}].action invalid for {kind}")
        if not isinstance(rid,str) or not rid.strip(): errors.append(f"reader_model_updates[{i}].id must be non-empty")
        for key in ("text","type","urgency","payoff_window"):
            if key in row and not isinstance(row.get(key),str): errors.append(f"reader_model_updates[{i}].{key} must be a string")
    return errors


def apply_updates(model: dict[str, Any], updates: list[dict[str, Any]], chapter: int) -> dict[str, Any]:
    out=deepcopy(model)
    errors=validate_model(out)+validate_updates(updates)
    if errors: raise ValueError("; ".join(errors))
    for row in updates:
        kind=row["kind"]; key=LIST_KEYS[kind]; rid=row["id"].strip(); action=row["action"]
        rows=out[key]; existing=next((x for x in rows if isinstance(x,dict) and x.get("id")==rid),None)
        if existing is None:
            text=str(row.get("text","")).strip()
            if not text: raise ValueError(f"new reader-model item {rid} requires text")
            existing={"id":rid,"text":text,"kind":kind,"introduced_chapter":chapter}
            rows.append(existing)
        elif row.get("text"):
            existing["text"]=str(row["text"]).strip()
        existing["status"]=action
        existing["last_action"]=action
        existing["last_touched_chapter"]=chapter
        for field in ("type","urgency","payoff_window"):
            if row.get(field) not in (None,""): existing[field]=row[field]
    out["updated_through_chapter"]=chapter
    return out


def active_view(model: dict[str, Any]) -> dict[str, Any]:
    result={"schema":1,"updated_through_chapter":model.get("updated_through_chapter",0)}
    for kind,key in LIST_KEYS.items():
        terminal=ACTIVE_TERMINAL[kind]
        result[key]=[row for row in model.get(key,[]) if isinstance(row,dict) and row.get("status") not in terminal]
    return result

def context_view(model: dict[str, Any], limit: int = 24) -> dict[str, Any]:
    active=active_view(model); rows=[]
    urgency_rank={"high":3,"medium":2,"low":1}
    for kind,key in LIST_KEYS.items():
        for row in active.get(key,[]):
            if isinstance(row,dict): rows.append((urgency_rank.get(str(row.get("urgency","")),0),int(row.get("last_touched_chapter",row.get("introduced_chapter",0)) or 0),kind,key,row))
    rows.sort(key=lambda item:(item[0],item[1]),reverse=True)
    selected=rows[:max(1,limit)]
    result={"schema":1,"updated_through_chapter":active.get("updated_through_chapter",0),"truncated":len(rows)>len(selected)}
    for key in LIST_KEYS.values(): result[key]=[]
    for _,_,_,key,row in selected: result[key].append(row)
    return result


def main()->int:
    parser=argparse.ArgumentParser(description="Inspect the compact reader-knowledge / promise model.")
    parser.add_argument("workspace",nargs="?",default=".")
    parser.add_argument("--all",action="store_true",help="Include paid/resolved/abandoned items")
    args=parser.parse_args(); root=Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        data=load_reader_model(root)
    except ValueError as exc: parser.error(str(exc))
    shown=data if args.all else active_view(data)
    print(json.dumps(shown,ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
