#!/usr/bin/env python3
from __future__ import annotations
import argparse,difflib,json
from pathlib import Path
from common import atomic_write_text,load_json,read_text,safe_workspace_path,validate_workspace_layout
from production_state import block_for_range,production_manifest_relative_from_current,validate_manifest,load_production_settings


def chars(text:str)->int:return sum(1 for ch in text if not ch.isspace())

def main()->int:
    p=argparse.ArgumentParser(description="Measure how much Flash raw prose survives canonical integration and recommend speculative lookahead.")
    p.add_argument("workspace",nargs="?",default="."); p.add_argument("--start",type=int,required=True); p.add_argument("--record",action="store_true")
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/"state/current.json",required=True)
        if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
        manifest=load_json(safe_workspace_path(root,production_manifest_relative_from_current(current),allow_missing=False),required=True)
        errs=validate_manifest(manifest)
        if errs: raise ValueError("; ".join(errs))
        block=block_for_range(manifest,a.start,a.start+4)
        if not isinstance(block,dict): raise ValueError("no production block for requested start")
        rows=[]
        for chapter,raw_rel in zip(range(a.start,a.start+5),block["outputs"]):
            raw_path=safe_workspace_path(root,raw_rel,allow_missing=False); can_path=safe_workspace_path(root,f"chapters/chapter-{chapter:04d}.md",allow_missing=False)
            raw=read_text(raw_path,required=True); can=read_text(can_path,required=True)
            sim=difflib.SequenceMatcher(None,raw,can,autojunk=False).ratio(); rc=chars(raw); cc=chars(can); retention=min(rc,cc)/max(1,max(rc,cc))
            rows.append({"chapter":chapter,"raw_chars":rc,"canonical_chars":cc,"similarity":round(sim,4),"length_retention":round(retention,4)})
        survival=sum((r["similarity"]*0.8+r["length_retention"]*0.2) for r in rows)/len(rows)
        review_rel=f"state/reviews/batch-{a.start:04d}-{a.start+4:04d}.json"; review=load_json(root/review_rel,default={})
        warnings=0; revised=False
        if isinstance(review,dict):
            clean=review.get("final_clean",{}); warnings=int(clean.get("warning_count",0)) if isinstance(clean,dict) and isinstance(clean.get("warning_count",0),int) else 0
            fr=review.get("first_reader",{}); revised=bool(fr.get("revision_applied")) if isinstance(fr,dict) else False
        settings=load_production_settings(root); current_look=settings["speculative_lookahead_blocks"]
        recommended=current_look
        if survival<0.66 or warnings>=4: recommended=max(1,current_look-1)
        elif survival>0.88 and warnings<=1 and not revised: recommended=min(10,current_look+1)
        payload={"block":f"{a.start}-{a.start+4}","canonical_survival_rate":round(survival,4),"integration_cost":round(1-survival,4),"final_clean_warnings":warnings,"reader_revision_applied":revised,"chapters":rows,"current_lookahead_blocks":current_look,"recommended_lookahead_blocks":recommended,"advisory_only":True}
        if a.record:
            path=safe_workspace_path(root,".novel/production/integration-metrics.jsonl",allow_missing=True); old=read_text(path) if path.exists() else ""; atomic_write_text(path,old+json.dumps(payload,ensure_ascii=False)+"\n")
    except (ValueError,json.JSONDecodeError) as exc:p.error(str(exc))
    print(json.dumps(payload,ensure_ascii=False)); return 0
if __name__=="__main__":raise SystemExit(main())
