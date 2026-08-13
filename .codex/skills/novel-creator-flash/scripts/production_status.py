#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from chapter_stats import analyze_chapter_text,load_length_settings
from common import load_json,read_text,safe_workspace_path,sha256_text,title_chapter_number,validate_workspace_layout
from production_state import production_manifest_relative_from_current,validate_manifest

REPORT_LIST_FIELDS=("newly_invented_details","character_micro_changes","new_promises","motifs_or_objects","strong_lines_or_moments","possible_continuity_risks")

def validate_report(data:object,assignment:dict)->list[str]:
    errors=[]
    if not isinstance(data,dict) or data.get("schema") not in (1,2): return ["report must be schema 1 or 2"]
    for key in ("writer","start_chapter","end_chapter"):
        if data.get(key)!=assignment.get(key): errors.append(f"report {key} does not match assignment")
    if data.get("schema")==1:
        for key in REPORT_LIST_FIELDS:
            value=data.get(key)
            if not isinstance(value,list) or any(not isinstance(x,str) for x in value): errors.append(f"report {key} must be a list of strings")
        return errors
    deltas=data.get("chapter_deltas")
    expected=list(range(assignment["start_chapter"],assignment["end_chapter"]+1))
    if not isinstance(deltas,list) or len(deltas)!=5: errors.append("schema 2 report chapter_deltas must contain exactly five entries")
    else:
        got=[]
        for i,row in enumerate(deltas):
            if not isinstance(row,dict): errors.append(f"chapter_deltas[{i}] must be an object"); continue
            got.append(row.get("chapter"))
            if not isinstance(row.get("summary",""),str): errors.append(f"chapter_deltas[{i}].summary must be a string")
            if not isinstance(row.get("current_patch",{}),dict): errors.append(f"chapter_deltas[{i}].current_patch must be an object")
            if not isinstance(row.get("reader_model_updates",[]),list): errors.append(f"chapter_deltas[{i}].reader_model_updates must be a list")
        if got!=expected: errors.append("chapter_deltas chapter sequence must exactly match assigned five chapters")
    interface=data.get("block_interface")
    if not isinstance(interface,dict): errors.append("schema 2 report block_interface must be an object")
    else:
        if not isinstance(interface.get("assumed_entry",{}),dict): errors.append("block_interface.assumed_entry must be an object")
        if not isinstance(interface.get("exit_state",{}),dict): errors.append("block_interface.exit_state must be an object")
        for key in ("must_carry_forward","plan_deviations","reader_now_believes","reader_now_wonders","soft_inventions","hard_inventions","creative_keep","possible_continuity_risks"):
            value=interface.get(key,[])
            if not isinstance(value,list) or any(not isinstance(x,str) for x in value): errors.append(f"block_interface.{key} must be a list of strings")
        adjud=interface.get("adjudications",[])
        if not isinstance(adjud,list): errors.append("block_interface.adjudications must be a list when present")
        else:
            for i,item in enumerate(adjud):
                if not isinstance(item,dict): errors.append(f"block_interface.adjudications[{i}] must be an object"); continue
                if item.get("key") not in {"must_carry_forward","plan_deviations","hard_inventions"}: errors.append(f"block_interface.adjudications[{i}].key invalid")
                if item.get("decision") not in {"accepted","rejected","deferred"}: errors.append(f"block_interface.adjudications[{i}].decision invalid")
    return errors

def report_risks(data:object)->list[str]:
    if not isinstance(data,dict): return []
    if data.get("schema")==2 and isinstance(data.get("block_interface"),dict):
        value=data["block_interface"].get("possible_continuity_risks",[]); return value if isinstance(value,list) else []
    value=data.get("possible_continuity_risks",[]); return value if isinstance(value,list) else []

def evaluate_assignment(root:Path,assignment:dict,settings:dict)->dict:
    chapter_results=[]; block_ready=True
    outputs=assignment.get("outputs"); expected_chapters=list(range(assignment["start_chapter"],assignment["end_chapter"]+1))
    if not isinstance(outputs,list) or len(outputs)!=len(expected_chapters): return {"writer":assignment.get("writer"),"range":f"{assignment.get('start_chapter')}-{assignment.get('end_chapter')}","ready":False,"chapters":[],"report":assignment.get("report"),"report_errors":["assignment outputs must contain exactly five paths"],"continuity_risks":[]}
    for chapter,output in zip(expected_chapters,outputs):
        path=safe_workspace_path(root,output,allow_missing=True); item={"chapter":chapter,"output":output,"exists":path.is_file()}
        if not path.is_file(): item["status"]="missing"; block_ready=False
        else:
            if getattr(path.stat(),"st_nlink",1)!=1: raise ValueError(f"hard-linked writer output is not allowed: {output}")
            text=read_text(path,required=True); heading=title_chapter_number(text); item["heading_chapter"]=heading
            if heading!=chapter: item["status"]="wrong_chapter_heading"; block_ready=False
            stats=analyze_chapter_text(text,settings); item.update(stats); item["sha256"]=sha256_text(text.rstrip()+"\n")
            if not stats["passes_minimum"]: block_ready=False
        chapter_results.append(item)
    report_rel=assignment.get("report"); report_path=safe_workspace_path(root,report_rel,allow_missing=True) if isinstance(report_rel,str) else None; report_errors=[]; report_data=None
    if report_path is None or not report_path.is_file(): report_errors=["report missing"]
    else:
        if getattr(report_path.stat(),"st_nlink",1)!=1: raise ValueError(f"hard-linked writer report is not allowed: {report_rel}")
        report_data=load_json(report_path,required=True); report_errors=validate_report(report_data,assignment)
    if report_errors:block_ready=False
    return {"writer":assignment.get("writer"),"range":f"{assignment['start_chapter']}-{assignment['end_chapter']}","ready":block_ready,"chapters":chapter_results,"report":report_rel,"report_schema":report_data.get("schema") if isinstance(report_data,dict) else None,"report_errors":report_errors,"continuity_risks":report_risks(report_data)}

def main()->int:
    parser=argparse.ArgumentParser(description="Check each independent five-chapter production block. Ready blocks may be integrated without waiting for the whole wave.")
    parser.add_argument("workspace",nargs="?",default="."); args=parser.parse_args(); root=Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/"state/current.json",required=True)
        if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
        manifest_rel=production_manifest_relative_from_current(current); manifest_path=safe_workspace_path(root,manifest_rel,allow_missing=False); manifest=load_json(manifest_path,required=True)
        errors=validate_manifest(manifest)
        if errors: raise ValueError("; ".join(errors))
        settings=load_length_settings(root); blocks=[]; all_risks=[]
        for assignment in manifest["assignments"]:
            block=evaluate_assignment(root,assignment,settings); blocks.append(block)
            for risk in block["continuity_risks"]:
                if isinstance(risk,str) and risk.strip(): all_risks.append(f"{block['range']}: {risk.strip()}")
        all_ready=all(block["ready"] for block in blocks); ready_blocks=[block["range"] for block in blocks if block["ready"]]
        first_unready=next((block["range"] for block in blocks if not block["ready"]),None)
    except (ValueError,json.JSONDecodeError) as exc: parser.error(str(exc))
    run=current.get("production_run", {}) if isinstance(current,dict) else {}
    remainder=run.get("main_agent_remainder") if isinstance(run,dict) else None
    print(json.dumps({"production_run":f"{manifest['start_chapter']}-{manifest['end_chapter']}","all_ready":all_ready,"ready_blocks":ready_blocks,"first_unready_block":first_unready,"blocks":blocks,"continuity_risk_detected":bool(all_risks),"continuity_risks":all_risks,"main_agent_remainder":remainder,"next":"按正史顺序处理已经 ready 的五章块；不需要等待整轮 all_ready。尚未 ready 的后续块只阻塞自己。1-4 章余数由主Agent写入 staging，若作品尚未结束则等待后续补足五章；只有真正结尾才显式使用 final-tail 审读。"},ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
