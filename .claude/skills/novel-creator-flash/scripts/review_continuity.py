#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from batch_review import chapter_key,current_prose_hash,load_review_record,validate_batch_review_record
from batch_state import load_active_review_unit
from common import atomic_write_json,load_json,utc_timestamp,validate_workspace_layout

def main()->int:
    p=argparse.ArgumentParser(description="Upgrade, complete, or inspect the Continuity Reviewer decision for the active formal review unit.")
    p.add_argument("workspace",nargs="?",default=".")
    g=p.add_mutually_exclusive_group(required=True); g.add_argument("--invoke",action="store_true"); g.add_argument("--complete",action="store_true")
    p.add_argument("--reason",action="append",default=[]); p.add_argument("--blocking-count",type=int,default=0); p.add_argument("--warning-count",type=int,default=0)
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/"state/current.json",required=True)
        if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
        unit=load_active_review_unit(root,current); path,record=load_review_record(root,unit)
        if not isinstance(record,dict): raise ValueError("prepare-review must run first")
        if record.get("finalized") is True: raise ValueError("finalized review cannot be changed")
        cont=record.get("continuity")
        if not isinstance(cont,dict): raise ValueError("continuity record missing")
        if a.invoke:
            if cont.get("decision")=="invoke": raise ValueError("continuity review is already invoked")
            reasons=[x.strip() for x in a.reason if x.strip()]
            if not reasons: raise ValueError("--invoke requires at least one --reason")
            cont.update({"decision":"invoke","reasons":reasons[:8],"status":"pending","checked_by":"","blocking_count":None,"warning_count":None,"reviewed_hashes":None})
        else:
            if cont.get("decision")!="invoke": raise ValueError("continuity review must be invoked before completion")
            if a.blocking_count<0 or a.warning_count<0: raise ValueError("counts must be non-negative")
            hashes={chapter_key(c):current_prose_hash(root,c) for c in range(unit["start_chapter"],unit["end_chapter"]+1)}
            cont.update({"status":"completed","checked_by":"novel-fast-continuity-reviewer","blocking_count":a.blocking_count,"warning_count":a.warning_count,"reviewed_hashes":hashes,"reviewed_at":utc_timestamp()})
        record["continuity"]=cont; errors=validate_batch_review_record(record,unit,require_finalized=False)
        if errors: raise ValueError("; ".join(errors))
        atomic_write_json(path,record)
    except ValueError as exc:p.error(str(exc))
    print(json.dumps({"updated":True,"continuity":cont,"output":path.relative_to(root).as_posix()},ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
