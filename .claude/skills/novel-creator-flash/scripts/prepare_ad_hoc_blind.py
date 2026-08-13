#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from common import atomic_write_json,atomic_write_text,read_text,safe_workspace_path,sha256_text,utc_timestamp,validate_workspace_layout

def chapter_text(root:Path,n:int)->str:
    staging=safe_workspace_path(root,f'.novel/staging/chapter-{n:04d}.md',allow_missing=True); formal=safe_workspace_path(root,f'chapters/chapter-{n:04d}.md',allow_missing=True); p=staging if staging.is_file() else formal
    if not p.is_file(): raise ValueError(f'chapter prose missing: {n}')
    return read_text(p,required=True).rstrip()+'\n'

def main()->int:
    p=argparse.ArgumentParser(description='Create a non-canonical blind packet for an early First Reader check without changing the formal five-chapter review cadence.')
    p.add_argument('workspace',nargs='?',default='.'); p.add_argument('--start',type=int,required=True); p.add_argument('--end',type=int); p.add_argument('--reader-brief',default='')
    a=p.parse_args(); end=a.end or a.start; root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root)
        if a.start<1 or end<a.start or end-a.start>4: raise ValueError('ad-hoc blind range must contain 1-5 contiguous chapters')
        brief=a.reader_brief.strip() or '作为普通目标读者自然阅读，只反馈当前材料带来的阅读体验，不补查大纲或正史答案。'
        if len(brief)>800: raise ValueError('reader brief too long')
        lines=['# Novel Creator Ad-hoc Blind Packet','',f'- range: {a.start}-{end}',f'- target_reader: {brief}','', '> 这不是正式 review/canon 单元，不改变五章审读节奏，也不写入正史。','']
        hashes={}
        for c in range(a.start,end+1):
            text=chapter_text(root,c); hashes[f'chapter-{c:04d}']=sha256_text(text); lines += [f'<!-- BLIND-CHAPTER-{c:04d} -->',text.rstrip(),'']
        payload='\n'.join(lines).rstrip()+'\n'; rel=f'.novel/blind-packets/ad-hoc-{a.start:04d}-{end:04d}.md'; out=safe_workspace_path(root,rel,allow_missing=True); out.parent.mkdir(parents=True,exist_ok=True); atomic_write_text(out,payload)
        rr=f'.novel/ad-hoc-reviews/ad-hoc-{a.start:04d}-{end:04d}.json'; record=safe_workspace_path(root,rr,allow_missing=True); record.parent.mkdir(parents=True,exist_ok=True); atomic_write_json(record,{'schema':1,'kind':'ad_hoc_blind','start_chapter':a.start,'end_chapter':end,'created_at':utc_timestamp(),'blind_packet':{'path':rel,'sha256':sha256_text(payload)},'prose_hashes':hashes,'first_reader':{'status':'pending','response':''}})
    except ValueError as exc:p.error(str(exc))
    print(json.dumps({'prepared':True,'range':f'{a.start}-{end}','blind_packet':rel,'record':rr,'note':'只调用 First Reader；不要把这次早读当成正式五章 review，也不要求 Pure Reader。'},ensure_ascii=False)); return 0
if __name__=='__main__': raise SystemExit(main())
