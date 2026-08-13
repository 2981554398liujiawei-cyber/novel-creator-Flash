#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path
import re
from typing import Any
from batch_state import review_record_relative, validate_review_unit
from common import load_json, read_text, safe_workspace_path, sha256_text
SHA256_RE=re.compile(r"^[0-9a-f]{64}$")
def chapter_key(number:int)->str:return f"chapter-{number:04d}"

def validate_batch_review_record(data:Any,batch:dict[str,Any],*,require_finalized:bool=False)->list[str]:
    if not isinstance(data,dict): return ["batch review record must be an object"]
    errors=[]
    if data.get('schema')!=1 or isinstance(data.get('schema'),bool): errors.append('batch review schema must be integer 1')
    errors.extend(validate_review_unit(batch,field='review unit'))
    if data.get('review_kind','batch')!=batch.get('review_kind','batch'): errors.append('batch review review_kind mismatch')
    for key in ('batch_id','start_chapter','end_chapter','batch_size'):
        if data.get(key)!=batch.get(key): errors.append(f"batch review {key} mismatch")
    for field in ('frozen_hashes','final_hashes'):
        value=data.get(field)
        if not isinstance(value,dict): errors.append(f"batch review {field} must be an object"); continue
        expected={chapter_key(n) for n in range(batch['start_chapter'],batch['end_chapter']+1)}
        if set(value)!=expected: errors.append(f"batch review {field} chapter keys do not match active batch")
        for key,digest in value.items():
            if not isinstance(digest,str) or SHA256_RE.fullmatch(digest) is None: errors.append(f"batch review {field}.{key} must be SHA-256")
    packet=data.get('blind_packet')
    if not isinstance(packet,dict): errors.append('batch review blind_packet must be an object')
    else:
        if not isinstance(packet.get('path'),str) or not packet['path'].startswith('.novel/blind-packets/') or not packet['path'].endswith('.md'): errors.append('blind_packet.path invalid')
        if not isinstance(packet.get('sha256'),str) or SHA256_RE.fullmatch(packet['sha256']) is None: errors.append('blind_packet.sha256 invalid')
    length_check=data.get('length_check')
    if not isinstance(length_check,dict) or length_check.get('hard_minimum_met') is not True: errors.append('hard chapter minimum must be met before review')
    first_reader=data.get('first_reader')
    if not isinstance(first_reader,dict): errors.append('batch review first_reader must be an object')
    else:
        if first_reader.get('status') not in {'pending','completed'}: errors.append('first_reader.status invalid')

        required_count=first_reader.get("required_count")
        if not isinstance(required_count,int) or isinstance(required_count,bool) or not 1<=required_count<=3: errors.append("batch review first_reader.required_count must be 1-3")
        available=first_reader.get("available_readers",[])
        if not isinstance(available,list): errors.append("batch review first_reader.available_readers must be a list")
        completed=first_reader.get("completed_readers",[])
        if not isinstance(completed,list) or any(not isinstance(x,str) or not x for x in completed): errors.append("batch review first_reader.completed_readers must be strings")
        if first_reader.get("status")=="completed" and isinstance(required_count,int) and len(completed)<required_count: errors.append("batch review reader panel has fewer completed readers than required")
        if first_reader.get('status')=='completed':
            if first_reader.get('verdict') not in {'strong','acceptable','weak'}: errors.append('first_reader.verdict invalid')
            if first_reader.get('ending_pull') not in {'strong','fair','weak','restful'}: errors.append('first_reader.ending_pull invalid')
            if not isinstance(first_reader.get('revision_applied'),bool): errors.append('first_reader.revision_applied must be boolean')
            tags=first_reader.get('issue_tags',[])
            if not isinstance(tags,list) or len(tags)>3: errors.append('first_reader.issue_tags invalid')
    pure=data.get('pure_reader')
    if not isinstance(pure,dict): errors.append('batch review pure_reader must be an object')
    else:
        if pure.get('status') not in {'pending','completed'}: errors.append('pure_reader.status invalid')
        if pure.get('status')=='completed' and (not isinstance(pure.get('response'),str) or not pure.get('response').strip()): errors.append("pure_reader.response must record the reader\'s natural reaction")
    adjudication=data.get('feedback_adjudication')
    if not isinstance(adjudication,dict): errors.append('batch review feedback_adjudication must be an object')
    else:
        if adjudication.get('status') not in {'pending','completed'}: errors.append('feedback_adjudication.status invalid')
        decisions=adjudication.get('decisions',[])
        if not isinstance(decisions,list) or len(decisions)>8: errors.append('feedback_adjudication.decisions must be a list with at most 8 items')
        else:
            for index,item in enumerate(decisions):
                if not isinstance(item,dict): errors.append(f'feedback_adjudication.decisions[{index}] must be an object'); continue
                if item.get('decision') not in {'accept','protect','defer','reject'}: errors.append(f'feedback_adjudication.decisions[{index}].decision invalid')
                if not isinstance(item.get('reason',''),str) or not item.get('reason','').strip(): errors.append(f'feedback_adjudication.decisions[{index}].reason required')
                if 'chapter' in item and (not isinstance(item.get('chapter'),int) or isinstance(item.get('chapter'),bool) or item.get('chapter')<1): errors.append(f'feedback_adjudication.decisions[{index}].chapter invalid')
                if 'location' in item and not isinstance(item.get('location'),str): errors.append(f'feedback_adjudication.decisions[{index}].location must be a string')
    cont=data.get('continuity')
    if not isinstance(cont,dict): errors.append('batch review continuity must be an object')
    else:
        decision=cont.get('decision')
        if decision not in {'invoke','skip'}: errors.append('continuity.decision must be invoke or skip')
        reasons=cont.get('reasons',[])
        if not isinstance(reasons,list) or len(reasons)>8 or any(not isinstance(x,str) or not x.strip() or len(x)>160 for x in reasons): errors.append('continuity.reasons invalid')
        if cont.get('status') not in {'pending','completed'}: errors.append('continuity.status invalid')
        if decision=='skip':
            if cont.get('status')!='completed' or cont.get('checked_by')!='main-agent' or cont.get('blocking_count')!=0: errors.append('skipped continuity review must record main-agent decision with zero blockers')
        elif cont.get('status')=='completed':
            if cont.get('checked_by')!='novel-fast-continuity-reviewer': errors.append('invoked continuity review must be checked by novel-fast-continuity-reviewer')
            for field in ('blocking_count','warning_count'):
                v=cont.get(field)
                if not isinstance(v,int) or isinstance(v,bool) or v<0: errors.append(f"continuity.{field} must be non-negative integer")
            reviewed=cont.get('reviewed_hashes')
            if reviewed is not None:
                expected={chapter_key(n) for n in range(batch['start_chapter'],batch['end_chapter']+1)}
                if not isinstance(reviewed,dict) or set(reviewed)!=expected or any(not isinstance(v,str) or SHA256_RE.fullmatch(v) is None for v in reviewed.values()): errors.append('continuity.reviewed_hashes must exactly cover the review unit with SHA-256 values')
    clean=data.get('final_clean')
    if not isinstance(clean,dict): errors.append('batch review final_clean must be an object')
    else:
        if clean.get('status') not in {'pending','needs_adjudication','clean'}: errors.append('final_clean.status invalid')
        if clean.get('status')=='clean' and clean.get('blocking_count')!=0: errors.append('final_clean clean status requires zero blockers')
        adjud=clean.get('warning_adjudications',[])
        if not isinstance(adjud,list): errors.append('final_clean.warning_adjudications must be a list')
        else:
            for i,item in enumerate(adjud):
                if not isinstance(item,dict): errors.append(f'final_clean.warning_adjudications[{i}] must be an object'); continue
                if item.get('decision') not in {'fixed','intentional_in_world'}: errors.append(f'final_clean.warning_adjudications[{i}].decision invalid')
                scope=item.get('scope','id')
                if scope not in {'id','group'}: errors.append(f'final_clean.warning_adjudications[{i}].scope invalid')
                if scope=='id':
                    if not isinstance(item.get('id'),str) or not item.get('id'): errors.append(f'final_clean.warning_adjudications[{i}].id required')
                else:
                    if item.get('decision')!='intentional_in_world': errors.append(f'final_clean.warning_adjudications[{i}] group decision must be intentional_in_world')
                    if not isinstance(item.get('category'),str) or not item.get('category'): errors.append(f'final_clean.warning_adjudications[{i}].category required for group')
                    ids=item.get('warning_ids')
                    if not isinstance(ids,list) or not ids or any(not isinstance(x,str) or not x for x in ids): errors.append(f'final_clean.warning_adjudications[{i}].warning_ids required for group')
                if not isinstance(item.get('reason',''),str) or not item.get('reason','').strip(): errors.append(f'final_clean.warning_adjudications[{i}].reason required')
    if not isinstance(data.get('finalized'),bool): errors.append('batch review finalized must be boolean')
    if require_finalized:
        if data.get('finalized') is not True: errors.append('batch review must be finalized')
        if isinstance(first_reader,dict) and first_reader.get('status')!='completed': errors.append('specialist blind reader review must be completed')
        if isinstance(pure,dict) and pure.get('status')!='completed': errors.append('pure blind reader must be completed')
        if isinstance(adjudication,dict) and adjudication.get('status')!='completed': errors.append('reader feedback adjudication must be completed')
        if isinstance(cont,dict):
            if cont.get('status')!='completed': errors.append('continuity decision/review must be completed')
            if cont.get('blocking_count')!=0: errors.append('continuity must have zero blocking issues')
            if cont.get('decision')=='invoke' and not isinstance(cont.get('reviewed_hashes'),dict): errors.append('invoked continuity review must record reviewed_hashes')
        if isinstance(clean,dict) and clean.get('status')!='clean': errors.append('final clean check must be clean')
    return errors

def load_review_record(root:Path,batch:dict[str,Any]):
    path=safe_workspace_path(root,review_record_relative(batch),allow_missing=True); data=load_json(path,default=None); return path,data if isinstance(data,dict) else data

def current_prose_hash(root:Path,chapter:int)->str:
    formal=safe_workspace_path(root,f"chapters/chapter-{chapter:04d}.md",allow_missing=True); staging=safe_workspace_path(root,f".novel/staging/chapter-{chapter:04d}.md",allow_missing=True); path=formal if formal.is_file() else staging
    if not path.is_file(): raise ValueError(f"batch review chapter file is missing: chapter-{chapter:04d}.md")
    return sha256_text(read_text(path,required=True).rstrip()+"\n")

def verify_final_hashes(root:Path,data:dict[str,Any],batch:dict[str,Any])->list[str]:
    errors=[]; final_hashes=data.get('final_hashes',{})
    if not isinstance(final_hashes,dict): return ['batch review final_hashes must be an object']
    for chapter in range(batch['start_chapter'],batch['end_chapter']+1):
        key=chapter_key(chapter)
        try: actual=current_prose_hash(root,chapter)
        except ValueError as exc: errors.append(str(exc)); continue
        if final_hashes.get(key)!=actual: errors.append(f"batch review final hash no longer matches {key}; rerun finalize-review after edits")
    return errors
