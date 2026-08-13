#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from common import load_json,safe_workspace_path,validate_workspace_layout
from production_state import block_for_range,production_manifest_relative_from_current,validate_manifest
from working_state import compile_working_state

FIELDS=("current_location","point_of_view","current_goal")

def actual_entry(root:Path)->dict:
    working=compile_working_state(root,write=True)
    current=load_json(root/"state/current.json",required=True)
    story=working.get("story_state",{}) if isinstance(working,dict) and isinstance(working.get("story_state"),dict) else (current if isinstance(current,dict) else {})
    return {"story":story,"reader_model":working.get("reader_model_preview",{}) if isinstance(working,dict) else {},"entity_overlays":working.get("entity_overlays",{}) if isinstance(working,dict) else {}}

def main()->int:
    p=argparse.ArgumentParser(description="Compare a speculative Flash block's assumed entry with the latest actual canonical/working state.")
    p.add_argument("workspace",nargs="?",default="."); p.add_argument("--start",type=int,required=True)
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/"state/current.json",required=True)
        if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
        manifest=load_json(safe_workspace_path(root,production_manifest_relative_from_current(current),allow_missing=False),required=True)
        errs=validate_manifest(manifest)
        if errs: raise ValueError("; ".join(errs))
        block=block_for_range(manifest,a.start,a.start+4)
        if not isinstance(block,dict): raise ValueError("no five-chapter assignment starts at requested chapter")
        report=load_json(safe_workspace_path(root,block["report"],allow_missing=False),required=True)
        interface=report.get("block_interface",{}) if isinstance(report,dict) else {}
        assumed=interface.get("assumed_entry",{}) if isinstance(interface,dict) else {}
        if not isinstance(assumed,dict): assumed={}
        actual_bundle=actual_entry(root); actual=actual_bundle.get("story",{}); mismatches=[]
        for field in FIELDS:
            av=assumed.get(field); bv=actual.get(field)
            if isinstance(av,str) and av.strip() and isinstance(bv,str) and av.strip()!=bv.strip(): mismatches.append({"field":field,"assumed":av,"actual":bv})
        abr=assumed.get("scene_bridge",{}); bbr=actual.get("scene_bridge",{})
        if isinstance(abr,dict) and isinstance(bbr,dict):
            for field in ("time","location","pov","last_action","immediate_pressure","emotional_residue"):
                av=abr.get(field); bv=bbr.get(field)
                if isinstance(av,str) and av.strip() and isinstance(bv,str) and av.strip()!=bv.strip(): mismatches.append({"field":"scene_bridge."+field,"assumed":av,"actual":bv})
        # Optional richer assumptions remain compact and are checked only when the Writer declared them.
        expected_promises=assumed.get("reader_promise_ids",[])
        if isinstance(expected_promises,list) and expected_promises:
            reader=actual_bundle.get("reader_model",{}); actual_ids=set()
            if isinstance(reader,dict):
                for key in ("promises","open_questions","beliefs","suspicions"):
                    for row in reader.get(key,[]) if isinstance(reader.get(key,[]),list) else []:
                        if isinstance(row,dict) and isinstance(row.get("id"),str): actual_ids.add(row["id"])
            for rid in expected_promises:
                if isinstance(rid,str) and rid not in actual_ids: mismatches.append({"field":"reader_promise_ids","assumed":rid,"actual":"not active"})
        expected_entities=assumed.get("entity_states",{})
        if isinstance(expected_entities,dict):
            overlays=actual_bundle.get("entity_overlays",{})
            for eid,patch in expected_entities.items():
                actual_patch=overlays.get(str(eid).upper(),{}) if isinstance(overlays,dict) else {}
                if isinstance(patch,dict):
                    for key,value in patch.items():
                        if key in actual_patch and actual_patch.get(key)!=value: mismatches.append({"field":f"entity_states.{eid}.{key}","assumed":value,"actual":actual_patch.get(key)})
        interface_decisions=interface.get("adjudications",[]) if isinstance(interface.get("adjudications",[]),list) else []
        undecided=[]
        for key in ("must_carry_forward","plan_deviations","hard_inventions"):
            items=interface.get(key,[]) if isinstance(interface.get(key,[]),list) else []
            decided={(x.get("key"),x.get("index")) for x in interface_decisions if isinstance(x,dict) and x.get("decision") in {"accepted","rejected","deferred"}}
            for idx,text in enumerate(items):
                if (key,idx) not in decided: undecided.append({"key":key,"index":idx,"text":text})
    except (ValueError,json.JSONDecodeError) as exc: p.error(str(exc))
    print(json.dumps({"block":f"{a.start}-{a.start+4}","rebase_required":bool(mismatches),"mismatches":mismatches,"interface_adjudication_required":bool(undecided),"undecided_interface_items":undecided,"instruction":"只修实际失配处；未来会影响后续的 hard invention / deviation / carry-forward 先由主Agent accepted/rejected/deferred 裁决，不要因为 rebase 全面重写五章。"},ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
