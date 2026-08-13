#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from common import atomic_write_json,chapter_filename,chapter_meta_filename,load_json,read_text,sha256_text,utc_timestamp,validate_workspace_layout,workspace_lock

def prose_hash(root:Path,chapter:int)->str|None:
    p=root/'chapters'/chapter_filename(chapter)
    return sha256_text(read_text(p,required=True)) if p.is_file() else None

def main()->int:
    p=argparse.ArgumentParser(description="Record the required semantic/structural review for a rewritten committed chapter.")
    p.add_argument('workspace',nargs='?',default='.'); p.add_argument('--chapter',type=int,required=True); p.add_argument('--blocking-count',type=int,default=0); p.add_argument('--warning-count',type=int,default=0); p.add_argument('--checked-by',required=True); p.add_argument('--structural-state-reconciled',action='store_true')
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        with workspace_lock(root):
            meta_path=root/'state/chapters'/chapter_meta_filename(a.chapter); meta=load_json(meta_path,required=True)
            if not isinstance(meta,dict) or not meta.get('prose_rewrite_review_required'): raise ValueError('chapter has no pending rewrite review')
            review=meta.get('rewrite_review',{}) if isinstance(meta.get('rewrite_review'),dict) else {}
            level=review.get('level','semantic')
            if level=='prose': raise ValueError('prose-only rewrite does not require Continuity Reviewer; confirm-rewrite runs final-clean directly')
            if a.blocking_count<0 or a.warning_count<0: raise ValueError('counts must be non-negative')
            hashes={}
            for c in range(max(1,a.chapter-1),a.chapter+2):
                h=prose_hash(root,c)
                if h is not None: hashes[f'chapter-{c:04d}']=h
            if level=='structural' and not a.structural_state_reconciled: raise ValueError('structural rewrite requires --structural-state-reconciled after impact/state reconciliation')
            review.update({'status':'reviewed','checked_by':a.checked_by,'blocking_count':a.blocking_count,'warning_count':a.warning_count,'reviewed_hashes':hashes,'reviewed_at':utc_timestamp(),'structural_state_reconciled':bool(a.structural_state_reconciled)})
            meta['rewrite_review']=review; atomic_write_json(meta_path,meta)
    except ValueError as exc:p.error(str(exc))
    print(json.dumps({'reviewed':True,'chapter':a.chapter,'level':level,'blocking_count':a.blocking_count,'reviewed_hashes':hashes},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
