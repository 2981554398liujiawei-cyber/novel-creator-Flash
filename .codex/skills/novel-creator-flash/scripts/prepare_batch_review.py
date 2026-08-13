#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
from batch_review import chapter_key, current_prose_hash
from batch_state import load_active_batch, load_active_review_unit, make_final_tail, review_record_relative
from blind_packet import build_blind_packet
from chapter_stats import analyze_chapter_text, load_length_settings
from common import atomic_write_json, load_json, read_text, safe_workspace_path, utc_timestamp, validate_workspace_layout
from production_state import block_for_range, load_production_settings, production_manifest_relative_from_current, reader_agents_for, validate_manifest
from production_status import evaluate_assignment, report_risks


def persisted_writer_risks(root: Path,current:dict,unit:dict)->list[str]:
    if unit.get('review_kind')=='final_tail': return []
    try: manifest_rel=production_manifest_relative_from_current(current)
    except ValueError: return []
    path=safe_workspace_path(root,manifest_rel,allow_missing=True)
    if not path.is_file(): return []
    manifest=load_json(path,required=True)
    if not isinstance(manifest,dict) or manifest.get('schema')!=2: return []
    out=[]
    for block in manifest.get('assignments',[]):
        if not isinstance(block,dict): continue
        bs,be,report_rel=block.get('start_chapter'),block.get('end_chapter'),block.get('report')
        if not isinstance(bs,int) or not isinstance(be,int) or not isinstance(report_rel,str): continue
        if be<unit['start_chapter'] or bs>unit['end_chapter']: continue
        report_path=safe_workspace_path(root,report_rel,allow_missing=True)
        if not report_path.is_file(): continue
        report=load_json(report_path,required=True); risks=report_risks(report)
        for item in risks:
            if isinstance(item,str) and item.strip(): out.append(f"{bs}-{be}: {item.strip()}")
    return list(dict.fromkeys(out))


def _assert_minimums(root:Path,unit:dict)->dict[str,int]:
    settings=load_length_settings(root); too=[]; measured={}
    for c in range(unit['start_chapter'],unit['end_chapter']+1):
        path=safe_workspace_path(root,f".novel/staging/chapter-{c:04d}.md",allow_missing=True)
        if not path.is_file(): path=safe_workspace_path(root,f"chapters/chapter-{c:04d}.md",allow_missing=True)
        if not path.is_file(): raise ValueError(f"chapter prose is missing: chapter-{c:04d}.md")
        stats=analyze_chapter_text(read_text(path,required=True),settings); measured[str(c)]=stats['effective_chars']
        if not stats['passes_minimum']: too.append((c,stats['effective_chars'],settings['minimum_effective_chars']))
    if too:
        raise ValueError('review cannot start before every chapter reaches the configured hard minimum: '+', '.join(f"chapter {c}: {n}/{m}" for c,n,m in too))
    return measured


def main()->int:
    parser=argparse.ArgumentParser(description='Freeze a fixed five-chapter Flash review unit, or a 1-4 chapter main-agent final tail.')
    parser.add_argument('workspace',nargs='?',default='.')
    parser.add_argument('--force',action='store_true')
    parser.add_argument('--continuity-review',choices=('invoke','skip'),required=True,help='Main Agent decides after each formal unit whether the independent Continuity Reviewer is useful')
    parser.add_argument('--continuity-reason',action='append',default=[])
    parser.add_argument('--reader-brief',default='')
    parser.add_argument('--source-mode',choices=('production','main-agent','imported'),help='Explicit prose source. main-agent needs no Writer manifest; imported is only for externally supplied prose.')
    parser.add_argument('--imported',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--final-tail-count',type=int,choices=(1,2,3,4))
    args=parser.parse_args(); root=Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        current_path=safe_workspace_path(root,'state/current.json',allow_missing=False); current=load_json(current_path,required=True)
        if not isinstance(current,dict): raise ValueError('state/current.json must be an object')
        latest=current.get('latest_chapter',0)
        if not isinstance(latest,int) or isinstance(latest,bool) or latest<0: raise ValueError('latest_chapter invalid')
        active=load_active_batch(root,current)
        if args.final_tail_count is not None:
            if latest+1!=active['start_chapter']: raise ValueError('final tail must start at the next uncommitted chapter')
            declared=make_final_tail(active,args.final_tail_count); existing=current.get('final_tail')
            if existing is not None and existing!=declared and not args.force: raise ValueError('a different final tail is already declared')
            current=dict(current); current['final_tail']=declared; atomic_write_json(current_path,current)
        unit=load_active_review_unit(root,current); measured=_assert_minimums(root,unit)
        if args.imported and args.source_mode not in (None,'imported'): raise ValueError('--imported conflicts with --source-mode')
        source_mode=args.source_mode or ('imported' if args.imported else None)
        if unit.get('review_kind')=='final_tail': source_mode='main-agent'
        if source_mode is None:
            provisional_sources=[]
            for c in range(unit['start_chapter'],unit['end_chapter']+1):
                d=load_json(safe_workspace_path(root,f'.novel/staging/deltas/chapter-{c:04d}.json',allow_missing=True),default=None)
                if isinstance(d,dict) and isinstance(d.get('prose_source'),str): provisional_sources.append(d['prose_source'])
            source_mode='main-agent' if len(provisional_sources)==unit['batch_size'] and set(provisional_sources)=={'main-agent'} else 'production'
        production_evidence=None
        if unit.get("review_kind")!="final_tail" and source_mode=='production':
            manifest_rel=production_manifest_relative_from_current(current)
            manifest_path=safe_workspace_path(root,manifest_rel,allow_missing=False); manifest=load_json(manifest_path,required=True)
            manifest_errors=validate_manifest(manifest)
            if manifest_errors: raise ValueError("invalid production manifest: "+"; ".join(manifest_errors))
            assignment=block_for_range(manifest,unit["start_chapter"],unit["end_chapter"])
            if assignment is None: raise ValueError("active five-chapter review unit has no matching production assignment; use --source-mode main-agent for 主Agent prose or --source-mode imported for external prose")
            block_status=evaluate_assignment(root,assignment,load_length_settings(root))
            if not block_status["ready"]: raise ValueError("matching Flash production block is not ready; finish its five raw chapters and writer report before review")
            production_evidence={"manifest":manifest_rel,"writer":assignment["writer"],"range":block_status["range"]}
        writer_risks=persisted_writer_risks(root,current,unit) if source_mode=='production' else []
        if writer_risks and args.continuity_review!='invoke': raise ValueError('Writer report contains persisted continuity risks; --continuity-review must be invoke')
        writer_reasons=[f"writer-report: {x}" for x in writer_risks]
        manual=[x.strip() for x in args.continuity_reason if x.strip()]
        reasons=list(dict.fromkeys(writer_reasons+manual))[:8]
        if any(len(x)>160 for x in reasons): raise ValueError('each continuity reason must be at most 160 characters')
        path=safe_workspace_path(root,review_record_relative(unit),allow_missing=True); existing=load_json(path,default=None)
        if existing is not None and not args.force: raise ValueError(f"review record already exists: {path.relative_to(root).as_posix()}")
        if isinstance(existing,dict) and existing.get('finalized') is True: raise ValueError('finalized review record cannot be replaced')
        production=load_production_settings(root)
        hashes={chapter_key(c):current_prose_hash(root,c) for c in range(unit['start_chapter'],unit['end_chapter']+1)}
        packet_path,packet_hash=build_blind_packet(root,unit,reader_brief=args.reader_brief)
        readers=list(reader_agents_for(production['blind_reader_count']))
        cont={"decision":args.continuity_review,"reasons":reasons,"writer_report_risk_count":len(writer_risks),"status":"pending" if args.continuity_review=='invoke' else 'completed',"checked_by":"" if args.continuity_review=='invoke' else 'main-agent',"blocking_count":None if args.continuity_review=='invoke' else 0,"warning_count":None if args.continuity_review=='invoke' else 0,"reviewed_hashes":None}
        record={"schema":1,"review_kind":unit.get('review_kind','batch'),"batch_id":unit['batch_id'],"start_chapter":unit['start_chapter'],"end_chapter":unit['end_chapter'],"batch_size":unit['batch_size'],"prepared_at":utc_timestamp(),"source_mode":source_mode,"production_evidence":production_evidence,"length_check":{"hard_minimum_met":True,"measured_effective_chars":measured},"frozen_hashes":hashes,"blind_packet":{"path":packet_path,"sha256":packet_hash},"first_reader":{"status":"pending","required_count":production['blind_reader_count'],"completed_readers":[],"available_readers":readers,"verdict":"","ending_pull":"","revision_applied":None,"issue_tags":[],"highest_value_revision":""},"pure_reader":{"status":"pending","response":""},"feedback_adjudication":{"status":"pending","decisions":[]},"continuity":cont,"final_clean":{"status":"pending","blocking_count":None,"warning_count":None,"checked_hashes":{},"warnings":[],"warning_adjudications":[]},"finalized":False,"finalized_at":None,"final_hashes":dict(hashes)}
        atomic_write_json(path,record)
    except (ValueError,json.JSONDecodeError) as exc: parser.error(str(exc))
    print(json.dumps({"prepared":True,"review_kind":unit.get('review_kind','batch'),"range":f"{unit['start_chapter']}-{unit['end_chapter']}","output":path.relative_to(root).as_posix(),"blind_packet":packet_path,"writer_report_risks":writer_risks,"source_mode":source_mode,"continuity_review":args.continuity_review,"next":"Run configured specialist blind readers plus novel-fast-pure-reader on the exact blind packet. If continuity_review=invoke, run novel-fast-continuity-reviewer. Revise, Prose Craft, then finalize-review; finalization performs the deterministic final clean scan."},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
