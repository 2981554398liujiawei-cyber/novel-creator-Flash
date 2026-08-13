#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from common import atomic_write_json,load_json,read_text,safe_workspace_path,sha256_text,validate_workspace_layout
from production_state import block_for_range,production_manifest_relative_from_current,validate_manifest
from production_status import validate_report
from working_state import scaffold,compile_working_state,provisional_relative


def main()->int:
    p=argparse.ArgumentParser(description='Adopt a reviewed Flash schema-2 Block Interface into provisional staging deltas after the main agent has integrated the raw prose.')
    p.add_argument('workspace',nargs='?',default='.'); p.add_argument('--start',type=int,required=True); p.add_argument('--force',action='store_true')
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/'state/current.json',required=True)
        if not isinstance(current,dict): raise ValueError('state/current.json must be an object')
        manifest=load_json(safe_workspace_path(root,production_manifest_relative_from_current(current),allow_missing=False),required=True)
        errs=validate_manifest(manifest)
        if errs: raise ValueError('; '.join(errs))
        block=block_for_range(manifest,a.start,a.start+4)
        if not isinstance(block,dict): raise ValueError('no production block starts at requested chapter')
        report=load_json(safe_workspace_path(root,block['report'],allow_missing=False),required=True)
        errs=validate_report(report,block)
        if errs: raise ValueError('; '.join(errs))
        if report.get('schema')!=2: raise ValueError('adopt-interface requires a schema 2 Writer report')
        deltas={row['chapter']:row for row in report['chapter_deltas'] if isinstance(row,dict) and isinstance(row.get('chapter'),int)}
        adopted=[]
        for chapter in range(a.start,a.start+5):
            staging=safe_workspace_path(root,f'.novel/staging/chapter-{chapter:04d}.md',allow_missing=False)
            if not staging.is_file(): raise ValueError(f'canonical staging must exist before interface adoption: chapter {chapter}')
            target=safe_workspace_path(root,provisional_relative(chapter),allow_missing=True)
            if not target.exists(): scaffold(root,chapter,False)
            elif not a.force: raise ValueError(f'provisional delta already exists for chapter {chapter}; use --force only after reviewing it')
            base=load_json(target,required=True); row=deltas[chapter]
            if not isinstance(base,dict): raise ValueError('provisional delta scaffold malformed')
            base['summary']=row.get('summary','')
            base['prose_source']='production'
            base['prose_sha256']=sha256_text(read_text(staging,required=True).rstrip()+'\n')
            base['reader_model_updates']=row.get('reader_model_updates',[])
            if isinstance(row.get('current_patch'),dict): base['current_patch']=row['current_patch']
            if isinstance(row.get('plan_deviations'),list) and row['plan_deviations']:
                base['dominant_change']=base.get('dominant_change') or '；'.join(str(x) for x in row['plan_deviations'][:2])
            atomic_write_json(target,base); compile_working_state(root,write=True); adopted.append(chapter)
    except (ValueError,json.JSONDecodeError) as exc:p.error(str(exc))
    print(json.dumps({'adopted':adopted,'working_state':'.novel/staging/working-state.json','note':'这是 provisional state；主Agent应按实际整合稿修正任何不准确 delta，再进入 review。'},ensure_ascii=False)); return 0
if __name__=='__main__':raise SystemExit(main())
