#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from common import atomic_write_json, deep_merge, first_heading, load_json, read_text, safe_workspace_path, sha256_text, title_chapter_number, validate_workspace_layout
from reader_model import apply_updates, context_view, load_reader_model, validate_updates


def provisional_relative(chapter:int)->str:
    return f".novel/staging/deltas/chapter-{chapter:04d}.json"


def _title(heading:str,chapter:int)->str:
    clean=heading.lstrip("#").strip()
    m=re.match(r"第\s*[零〇一二两三四五六七八九十百千万\d]+\s*章\s*(.*)$",clean)
    if m: return m.group(1).strip() or f"第{chapter}章"
    m=re.match(r"Chapter\s+\d+\s*(.*)$",clean,re.I)
    if m: return m.group(1).strip() or f"Chapter {chapter}"
    return clean


def validate_provisional(data:Any,chapter:int)->list[str]:
    if not isinstance(data,dict) or data.get("schema")!=1: return [f"provisional chapter {chapter} delta must be schema 1"]
    errors=[]
    if data.get("chapter")!=chapter: errors.append(f"provisional delta chapter mismatch for {chapter}")
    patch=data.get("current_patch")
    if not isinstance(patch,dict): errors.append("provisional current_patch must be an object")
    else:
        for key in ("current_location","point_of_view","current_goal"):
            if key in patch and not isinstance(patch.get(key),str): errors.append(f"provisional current_patch.{key} must be a string")
        if "scene_entities" in patch and (not isinstance(patch.get("scene_entities"),list) or any(not isinstance(x,str) for x in patch["scene_entities"])):
            errors.append("provisional current_patch.scene_entities must be a list of strings")
        bridge=patch.get("scene_bridge")
        if bridge is not None and not isinstance(bridge,dict): errors.append("provisional current_patch.scene_bridge must be an object")
    source=data.get("prose_source","main-agent")
    if source not in {"main-agent","production","imported"}: errors.append("provisional prose_source must be main-agent, production, or imported")
    digest=data.get("prose_sha256")
    if digest is not None and (not isinstance(digest,str) or re.fullmatch(r"[0-9a-f]{64}",digest) is None): errors.append("provisional prose_sha256 must be SHA-256 when present")
    errors.extend(validate_updates(data.get("reader_model_updates",[])))
    return errors


def contiguous_provisionals(root:Path,current:dict[str,Any])->list[dict[str,Any]]:
    latest=current.get("latest_chapter",0)
    if not isinstance(latest,int) or isinstance(latest,bool) or latest<0: raise ValueError("state/current.json latest_chapter invalid")
    rows=[]; chapter=latest+1
    while True:
        path=safe_workspace_path(root,provisional_relative(chapter),allow_missing=True)
        if not path.is_file(): break
        data=load_json(path,required=True); errors=validate_provisional(data,chapter)
        if errors: raise ValueError("; ".join(errors))
        prose_path=safe_workspace_path(root,f".novel/staging/chapter-{chapter:04d}.md",allow_missing=True)
        if not prose_path.is_file(): raise ValueError(f"provisional delta exists but staging prose is missing for chapter {chapter}")
        expected_hash=data.get("prose_sha256")
        if isinstance(expected_hash,str):
            actual_hash=sha256_text(read_text(prose_path,required=True).rstrip()+"\n")
            if actual_hash!=expected_hash:
                raise ValueError(f"provisional delta is stale for chapter {chapter}; staging prose changed after the delta was recorded")
        rows.append(data); chapter+=1
    return rows


def compile_working_state(root:Path, *, write:bool=True)->dict[str,Any]:
    current=load_json(root/"state/current.json",required=True)
    if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
    reader=load_reader_model(root)
    state=dict(current); staged=[]; model=reader; entity_overlays={}
    for delta in contiguous_provisionals(root,current):
        chapter=delta["chapter"]
        state=deep_merge(state,delta.get("current_patch",{}))
        state["recent_summary"]=delta.get("summary") or state.get("recent_summary","")
        if delta.get("outline_node"): state["outline_node"]=delta["outline_node"]
        state["latest_staged_chapter"]=chapter
        staged.append(chapter)
        model=apply_updates(model,delta.get("reader_model_updates",[]),chapter)
        for change in delta.get("entity_changes",[]):
            if not isinstance(change,dict) or not isinstance(change.get("id"),str) or not isinstance(change.get("patch"),dict): continue
            eid=change["id"].upper(); entity_overlays[eid]=deep_merge(entity_overlays.get(eid,{}),change["patch"])
    payload={
        "schema":1,
        "base_committed_chapter":current.get("latest_chapter",0),
        "latest_staged_chapter":staged[-1] if staged else current.get("latest_chapter",0),
        "provisional_chapters":staged,
        "story_state":{k:state.get(k) for k in ("current_arc","outline_node","current_goal","current_location","point_of_view","scene_entities","arc_entities","scene_bridge","recent_summary","open_promises") if k in state},
        "reader_model_preview":context_view(model),
        "entity_overlays":entity_overlays,
    }
    path=safe_workspace_path(root,".novel/staging/working-state.json",allow_missing=True)
    if write:
        if staged: atomic_write_json(path,payload)
        elif path.exists(): path.unlink()
    return payload


def scaffold(root:Path,chapter:int,force:bool)->dict[str,Any]:
    current=load_json(root/"state/current.json",required=True)
    if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
    state=compile_working_state(root,write=True)
    expected=int(state.get("latest_staged_chapter",current.get("latest_chapter",0)))+1
    if chapter!=expected: raise ValueError(f"provisional chapters must be sequential; expected {expected}, got {chapter}")
    prose_path=safe_workspace_path(root,f".novel/staging/chapter-{chapter:04d}.md",allow_missing=False)
    prose=read_text(prose_path,required=True); heading=first_heading(prose)
    if title_chapter_number(prose)!=chapter or not heading: raise ValueError(f"staging prose must begin with chapter {chapter} Markdown heading")
    target=safe_workspace_path(root,provisional_relative(chapter),allow_missing=True)
    if target.exists() and not force: raise ValueError(f"provisional delta already exists: {target.relative_to(root).as_posix()}")
    base=state.get("story_state",{}) if isinstance(state.get("story_state"),dict) else {}
    bridge=base.get("scene_bridge",{}) if isinstance(base.get("scene_bridge"),dict) else {}
    data={
      "schema":1,"chapter":chapter,"title":_title(heading,chapter),"summary":"","prose_source":"main-agent","prose_sha256":sha256_text(prose.rstrip()+"\n"),
      "outline_node":str(base.get("outline_node",current.get("outline_node",""))),"chapter_function":"","dominant_change":"","reader_expectation_added":"",
      "reader_model_updates":[],"entities":[],"events":[],"depends_on_events":[],"knowledge_used":{},"state_used":[],"entity_changes":[],
      "current_patch":{
        "current_location":str(base.get("current_location","")),"point_of_view":str(base.get("point_of_view","")),
        "scene_entities":list(base.get("scene_entities",[])) if isinstance(base.get("scene_entities",[]),list) else [],
        "current_goal":str(base.get("current_goal","")),
        "scene_bridge":{k:str(bridge.get(k,"")) for k in ("time","location","pov","last_action","immediate_pressure","emotional_residue")},
      },
    }
    atomic_write_json(target,data); compile_working_state(root,write=True)
    return data


def main()->int:
    parser=argparse.ArgumentParser(description="Maintain provisional per-chapter deltas and compile the non-canonical working state.")
    parser.add_argument("workspace",nargs="?",default=".")
    parser.add_argument("--chapter",type=int)
    parser.add_argument("--scaffold",action="store_true")
    parser.add_argument("--force",action="store_true")
    args=parser.parse_args(); root=Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        if args.scaffold:
            if not isinstance(args.chapter,int) or args.chapter<1: raise ValueError("--scaffold requires positive --chapter")
            scaffold(root,args.chapter,args.force)
        payload=compile_working_state(root,write=True)
    except ValueError as exc: parser.error(str(exc))
    print(json.dumps({"updated":True,"working_state":".novel/staging/working-state.json" if payload.get("provisional_chapters") else None,**payload},ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
