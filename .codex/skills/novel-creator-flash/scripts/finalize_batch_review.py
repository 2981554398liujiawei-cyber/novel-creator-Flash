#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from batch_review import chapter_key,current_prose_hash,load_review_record,validate_batch_review_record
from batch_state import load_active_review_unit
from blind_packet import validate_blind_packet_record
from common import atomic_write_json,load_json,utc_timestamp,validate_workspace_layout
from final_clean_check import scan_review_unit

def main()->int:
    parser=argparse.ArgumentParser(description='Finalize the revised review unit after blind reading, continuity decision and deterministic final clean check.')
    parser.add_argument('workspace',nargs='?',default='.')
    args=parser.parse_args(); root=Path(args.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current=load_json(root/'state/current.json',required=True)
        if not isinstance(current,dict): raise ValueError('state/current.json must be an object')
        unit=load_active_review_unit(root,current); path,record=load_review_record(root,unit)
        if not isinstance(record,dict): raise ValueError(f"batch review record is missing: {path.relative_to(root).as_posix()}")
        errors=validate_batch_review_record(record,unit,require_finalized=False); errors.extend(validate_blind_packet_record(root,record.get('blind_packet')))
        if errors: raise ValueError('; '.join(errors))
        if record.get('first_reader',{}).get('status')!='completed': raise ValueError('specialist blind reader review is not completed')
        if record.get('pure_reader',{}).get('status')!='completed': raise ValueError('pure blind reader is not completed')
        if record.get('feedback_adjudication',{}).get('status')!='completed': raise ValueError('reader feedback has not been adjudicated by 主Agent')
        cont=record.get('continuity',{})
        if cont.get('status')!='completed' or cont.get('blocking_count')!=0: raise ValueError('continuity decision/review is incomplete or still has blockers')
        if cont.get('decision')=='invoke':
            reviewed=cont.get('reviewed_hashes')
            current_hashes={chapter_key(c):current_prose_hash(root,c) for c in range(unit['start_chapter'],unit['end_chapter']+1)}
            if not isinstance(reviewed,dict) or reviewed!=current_hashes:
                raise ValueError('candidate prose changed after Continuity Reviewer; rerun the continuity review on the current candidate before finalizing')
        clean=scan_review_unit(root,unit)
        previous_clean=record.get('final_clean',{}) if isinstance(record.get('final_clean'),dict) else {}
        adjudications=previous_clean.get('warning_adjudications',[]) if isinstance(previous_clean.get('warning_adjudications',[]),list) else []
        if clean['blocking']:
            record['final_clean']={'status':'pending','blocking_count':len(clean['blocking']),'warning_count':len(clean['warnings']),'checked_hashes':clean['checked_hashes'],'warnings':clean['warnings'],'warning_adjudications':adjudications}
            atomic_write_json(path,record)
            examples='; '.join(f"chapter {x['chapter']} line {x['line']} {x['category']}: {x['evidence']}" for x in clean['blocking'][:5])
            raise ValueError('final clean check found blocking finished-prose residue; fix prose and rerun finalize-review: '+examples)
        current_by_id={x.get('id'):x for x in clean['warnings'] if isinstance(x,dict) and isinstance(x.get('id'),str)}
        accepted_ids=set(); accepted_rows=[]
        for item in adjudications:
            if not isinstance(item,dict): continue
            scope=item.get('scope','id'); decision=item.get('decision')
            if scope=='group':
                ids=item.get('warning_ids',[])
                if decision!='intentional_in_world' or item.get('checked_hashes')!=clean['checked_hashes'] or not isinstance(ids,list): continue
                valid=[]
                for wid in ids:
                    warning=current_by_id.get(wid)
                    if warning is None: continue
                    if warning.get('category')!=item.get('category'): continue
                    if item.get('chapter') is not None and warning.get('chapter')!=item.get('chapter'): continue
                    valid.append(wid)
                if valid:
                    accepted_ids.update(valid); accepted_rows.append(item)
                continue
            wid=item.get('id')
            if not isinstance(wid,str): continue
            if decision=='fixed':
                if wid in current_by_id: continue
                accepted_rows.append(item)
            elif decision=='intentional_in_world' and wid in current_by_id and item.get('checked_hashes')==clean['checked_hashes']:
                accepted_ids.add(wid); accepted_rows.append(item)
        unresolved=[w for wid,w in current_by_id.items() if wid not in accepted_ids]
        record['final_clean']={'status':'needs_adjudication' if unresolved else 'clean','blocking_count':0,'warning_count':len(clean['warnings']),'checked_hashes':clean['checked_hashes'],'warnings':clean['warnings'],'warning_adjudications':accepted_rows}
        if unresolved:
            atomic_write_json(path,record)
            examples='; '.join(f"{x['id']} chapter {x['chapter']} line {x['line']} {x['category']}: {x['evidence']}" for x in unresolved[:6])
            raise ValueError('semantic final-clean warnings require adjudication: fix the prose, adjudicate one item with --id, or when every current warning in the same category is genuinely in-world use --category <CATEGORY> [--chapter N]. Group receipts cover only this scan/hash. Pending: '+examples)
        record['final_hashes']={chapter_key(c):current_prose_hash(root,c) for c in range(unit['start_chapter'],unit['end_chapter']+1)}
        record['finalized']=True; record['finalized_at']=utc_timestamp()
        errors=validate_batch_review_record(record,unit,require_finalized=True)
        if errors: raise ValueError('; '.join(errors))
        atomic_write_json(path,record)
    except ValueError as exc: parser.error(str(exc))
    print(json.dumps({'finalized':True,'review_kind':unit.get('review_kind','batch'),'range':f"{unit['start_chapter']}-{unit['end_chapter']}",'output':path.relative_to(root).as_posix(),'final_clean_warnings':record['final_clean']['warning_count']},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
