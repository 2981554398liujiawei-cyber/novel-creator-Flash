#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from common import atomic_write_json,load_json,safe_workspace_path,utc_timestamp,validate_workspace_layout
from production_state import block_for_range,production_manifest_relative_from_current,validate_manifest
from production_status import validate_report

def main()->int:
    p=argparse.ArgumentParser(description='Record main-agent decisions for future-affecting Flash Block Interface items.')
    p.add_argument('workspace',nargs='?',default='.'); p.add_argument('--start',type=int,required=True); p.add_argument('--key',choices=('must_carry_forward','plan_deviations','hard_inventions'),required=True); p.add_argument('--index',type=int,required=True); p.add_argument('--decision',choices=('accepted','rejected','deferred'),required=True); p.add_argument('--reason',required=True)
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/'state/current.json',required=True); manifest=load_json(safe_workspace_path(root,production_manifest_relative_from_current(current),allow_missing=False),required=True); errs=validate_manifest(manifest)
        if errs: raise ValueError('; '.join(errs))
        block=block_for_range(manifest,a.start,a.start+4)
        if not isinstance(block,dict): raise ValueError('production block not found')
        report_path=safe_workspace_path(root,block['report'],allow_missing=False); report=load_json(report_path,required=True); errs=validate_report(report,block)
        if errs: raise ValueError('; '.join(errs))
        if report.get('schema')!=2: raise ValueError('interface adjudication requires schema 2 report')
        interface=report['block_interface']; items=interface.get(a.key,[])
        if not isinstance(items,list) or a.index<0 or a.index>=len(items): raise ValueError('item index out of range')
        decisions=interface.get('adjudications',[]) if isinstance(interface.get('adjudications',[]),list) else []
        decisions=[x for x in decisions if not (isinstance(x,dict) and x.get('key')==a.key and x.get('index')==a.index)]
        decisions.append({'key':a.key,'index':a.index,'text':items[a.index],'decision':a.decision,'reason':a.reason.strip(),'decided_at':utc_timestamp()}); interface['adjudications']=decisions; atomic_write_json(report_path,report)
    except ValueError as exc:p.error(str(exc))
    print(json.dumps({'updated':True,'block':f'{a.start}-{a.start+4}','key':a.key,'index':a.index,'decision':a.decision},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
