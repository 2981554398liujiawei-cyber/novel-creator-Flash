#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from batch_review import load_review_record
from batch_state import load_active_review_unit
from common import atomic_write_json,load_json,utc_timestamp,validate_workspace_layout


def main()->int:
    p=argparse.ArgumentParser(description="Adjudicate semantic final-clean warnings from the most recent scan. Group adjudication covers only warning IDs in the current prose hashes; it never creates a permanent allowlist.")
    p.add_argument("workspace",nargs="?",default=".")
    target=p.add_mutually_exclusive_group(required=True)
    target.add_argument("--id")
    target.add_argument("--category")
    p.add_argument("--chapter",type=int,help="Optional chapter restriction when adjudicating a category group")
    p.add_argument("--decision",choices=("fixed","intentional_in_world"),required=True)
    p.add_argument("--reason",required=True)
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    if len(a.reason.strip())<4: p.error("--reason must be meaningful")
    if a.category and a.decision!='intentional_in_world': p.error("group adjudication is only for intentional_in_world; if prose was fixed, rerun finalize-review so the warnings disappear")
    if a.chapter is not None and a.chapter<1: p.error("--chapter must be positive")
    try:
        validate_workspace_layout(root); current=load_json(root/"state/current.json",required=True)
        if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
        unit=load_active_review_unit(root,current); path,record=load_review_record(root,unit)
        if not isinstance(record,dict): raise ValueError("prepare-review must run first")
        clean=record.get("final_clean",{}) if isinstance(record.get("final_clean"),dict) else {}
        warnings=clean.get("warnings",[]) if isinstance(clean.get("warnings",[]),list) else []
        rows=clean.get("warning_adjudications",[]) if isinstance(clean.get("warning_adjudications",[]),list) else []
        hashes=dict(clean.get("checked_hashes",{})) if isinstance(clean.get("checked_hashes"),dict) else {}
        if a.id:
            warning=next((x for x in warnings if isinstance(x,dict) and x.get("id")==a.id),None)
            if warning is None: raise ValueError("warning id is not present in the latest final-clean scan; rerun finalize-review first")
            rows=[x for x in rows if not (isinstance(x,dict) and x.get("scope","id")=="id" and x.get("id")==a.id)]
            rows.append({"scope":"id","id":a.id,"decision":a.decision,"reason":a.reason.strip(),"chapter":warning.get("chapter"),"category":warning.get("category"),"adjudicated_at":utc_timestamp(),"checked_hashes":hashes})
            result={"adjudicated":True,"scope":"id","id":a.id,"decision":a.decision,"covered":1}
        else:
            matches=[x for x in warnings if isinstance(x,dict) and x.get("category")==a.category and (a.chapter is None or x.get("chapter")==a.chapter) and isinstance(x.get("id"),str)]
            if not matches: raise ValueError("no warnings in the latest final-clean scan match that category/chapter")
            ids=sorted({x["id"] for x in matches})
            rows=[x for x in rows if not (isinstance(x,dict) and x.get("scope")=="group" and x.get("category")==a.category and x.get("chapter")==a.chapter)]
            rows.append({"scope":"group","category":a.category,"chapter":a.chapter,"warning_ids":ids,"decision":"intentional_in_world","reason":a.reason.strip(),"adjudicated_at":utc_timestamp(),"checked_hashes":hashes})
            result={"adjudicated":True,"scope":"group","category":a.category,"chapter":a.chapter,"decision":"intentional_in_world","covered":len(ids)}
        clean["warning_adjudications"]=rows; record["final_clean"]=clean; atomic_write_json(path,record)
    except ValueError as exc:p.error(str(exc))
    result["output"]=path.relative_to(root).as_posix(); print(json.dumps(result,ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
