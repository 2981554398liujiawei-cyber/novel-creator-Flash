#!/usr/bin/env python3
from __future__ import annotations
import argparse,hashlib,json
from pathlib import Path
from common import atomic_write_json,chapter_filename,chapter_meta_filename,ensure_no_symlink_chain,load_json,read_text,sha256_text,utc_timestamp,validate_workspace_layout,workspace_lock
from final_clean_check import scan_text

def main()->int:
    p=argparse.ArgumentParser(description="Confirm a rewritten committed chapter after the review required by its rewrite level.")
    p.add_argument("workspace",nargs="?",default="."); p.add_argument("--chapter",type=int,required=True); p.add_argument("--note",required=True)
    a=p.parse_args();
    if len(a.note.strip())<8:p.error("--note must briefly record the review conclusion")
    root=Path(a.workspace).resolve(strict=True); validate_workspace_layout(root)
    try:
      with workspace_lock(root):
        if (root/'.novel/transaction.json').exists(): raise ValueError('an unfinished transaction exists; recover it first')
        meta_path=root/'state/chapters'/chapter_meta_filename(a.chapter); prose_path=root/'chapters'/chapter_filename(a.chapter); meta=load_json(meta_path,required=True)
        if not isinstance(meta,dict) or not meta.get('prose_rewrite_review_required'): raise ValueError('chapter has no pending prose rewrite review')
        prose=read_text(prose_path,required=True); digest=sha256_text(prose)
        if str(meta.get('prose_sha256',''))!=digest: raise ValueError('chapter prose changed after rewrite; review the current file before confirming')
        review=meta.get('rewrite_review',{}) if isinstance(meta.get('rewrite_review'),dict) else {}; level=review.get('level','semantic')
        history=meta.get('revision_history',[]); previous=review.get('archive_record_revision'); record=next((x for x in history if isinstance(x,dict) and x.get('revision')==previous),None)
        if record is None: raise ValueError('previous revision record is missing')
        for pf,hf in (("archive_prose","prose_sha256"),("archive_metadata","metadata_sha256")):
            path=root/str(record.get(pf,'')); ensure_no_symlink_chain(path,root,allow_missing=False)
            try:path.resolve().relative_to((root/'revisions').resolve())
            except ValueError: raise ValueError('revision archive path is outside revisions/')
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=str(record.get(hf,'')): raise ValueError(f'previous revision archive is invalid: {pf}')
        if level=='prose':
            blockers,_=scan_text(prose,a.chapter)
            if blockers: raise ValueError('prose-only rewrite still contains final-clean blockers; fix them before confirmation')
        else:
            if review.get('status')!='reviewed' or review.get('blocking_count')!=0 or review.get('checked_by')!='novel-fast-continuity-reviewer': raise ValueError('semantic/structural rewrite requires a completed zero-blocker Continuity Reviewer receipt')
            expected={}
            for c in range(max(1,a.chapter-1),a.chapter+2):
                pp=root/'chapters'/chapter_filename(c)
                if pp.is_file(): expected[f'chapter-{c:04d}']=sha256_text(read_text(pp,required=True))
            if review.get('reviewed_hashes')!=expected: raise ValueError('rewrite or neighboring prose changed after semantic review; rerun review-rewrite')
            if level=='structural':
                receipt=root/'audits'/f'rewrite-impact-{a.chapter:04d}.json'; impact=load_json(receipt,required=True)
                if not isinstance(impact,dict) or impact.get('prose_sha256')!=digest: raise ValueError('structural rewrite requires rewrite-impact for the current prose hash')
                if review.get('structural_state_reconciled') is not True: raise ValueError('structural rewrite state has not been explicitly reconciled')
        review.update({"status":"confirmed","confirmed_at":utc_timestamp(),"note":a.note.strip()}); meta['rewrite_review']=review; meta['prose_rewrite_review_required']=False; atomic_write_json(meta_path,meta)
    except ValueError as exc:p.error(str(exc))
    print(json.dumps({"confirmed":True,"chapter":a.chapter,"revision":meta.get('revision'),"level":level},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
