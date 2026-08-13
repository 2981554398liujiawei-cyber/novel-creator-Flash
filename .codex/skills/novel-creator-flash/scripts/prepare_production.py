#!/usr/bin/env python3
from __future__ import annotations
import argparse,json
from pathlib import Path
from batch_state import REVIEW_BATCH_SIZE,load_active_batch
from common import atomic_write_json,load_json,safe_workspace_path,utc_timestamp,validate_workspace_layout
from production_state import WRITER_BLOCK_SIZE,build_assignments,load_production_settings,production_run_relative

def contiguous_staging_count(root:Path,start:int,end:int)->int:
    count=0
    for chapter in range(start,end+1):
        if not safe_workspace_path(root,f".novel/staging/chapter-{chapter:04d}.md",allow_missing=True).is_file(): break
        count+=1
    return count

def main()->int:
    p=argparse.ArgumentParser(description="Prepare one Flash wavefront. The pool may have 10 seats, but normal production leads canon only by the configured number of five-chapter blocks.")
    p.add_argument("workspace",nargs="?",default="."); p.add_argument("--writers",type=int); p.add_argument("--chapters",type=int); p.add_argument("--lookahead-blocks",type=int); p.add_argument("--allow-deep-speculation",action="store_true"); p.add_argument("--force",action="store_true")
    a=p.parse_args(); root=Path(a.workspace).resolve(strict=True)
    try:
        validate_workspace_layout(root); current_path=safe_workspace_path(root,"state/current.json",allow_missing=False); current=load_json(current_path,required=True)
        if not isinstance(current,dict): raise ValueError("state/current.json must be an object")
        latest=current.get("latest_chapter",0)
        if not isinstance(latest,int) or isinstance(latest,bool) or latest<0: raise ValueError("latest_chapter invalid")
        batch=load_active_batch(root,current)
        if batch["start_chapter"]!=latest+1: raise ValueError("active review batch must start at the next uncommitted chapter")
        if isinstance(current.get("final_tail"),dict): raise ValueError("finish active final_tail first")
        settings=load_production_settings(root); seat_cap=a.writers if a.writers is not None else settings["writer_pool_size"]
        if not isinstance(seat_cap,int) or isinstance(seat_cap,bool) or not 1<=seat_cap<=settings["writer_pool_size"]: raise ValueError(f"--writers must be 1-{settings['writer_pool_size']}")
        requested=a.chapters if a.chapters is not None else seat_cap*5
        if not isinstance(requested,int) or isinstance(requested,bool) or requested<1: raise ValueError("--chapters must be positive")
        lookahead=a.lookahead_blocks if a.lookahead_blocks is not None else settings["speculative_lookahead_blocks"]
        if not isinstance(lookahead,int) or isinstance(lookahead,bool) or not 1<=lookahead<=10: raise ValueError("--lookahead-blocks must be 1-10")
        if a.allow_deep_speculation: lookahead=seat_cap
        old=current.get("production_run")
        if isinstance(old,dict):
            old_end=old.get("end_chapter")
            if isinstance(old_end,int) and latest<old_end and not a.force: raise ValueError("an unfinished production wave exists")
        active_start=latest+1; active_end=active_start+4; existing=contiguous_staging_count(root,active_start,active_end); next_new=active_start+existing
        prefix_count=min(requested,5-existing) if existing else 0
        prefix={"writer":"main-agent","start_chapter":next_new,"end_chapter":next_new+prefix_count-1,"chapter_count":prefix_count,"reason":"complete_existing_partial_review_unit"} if prefix_count else None
        after=max(0,requested-prefix_count)
        requested_blocks=after//5
        writer_blocks=min(requested_blocks,seat_cap,lookahead)
        writer_chapters=writer_blocks*5
        writer_start=active_start if not existing else active_start+5
        writer_end=writer_start+writer_chapters-1 if writer_chapters else None
        scheduled=prefix_count+writer_chapters
        # A short request with no complete Writer block remains a main-agent partial unit. Once a wave hits the lookahead cap, later requested chapters are deferred rather than written speculatively.
        remainder_count=after-writer_chapters if requested_blocks<=writer_blocks else 0
        remainder=None
        if remainder_count and remainder_count<5:
            rs=(writer_end+1) if writer_end is not None else (next_new+prefix_count); remainder={"writer":"main-agent","start_chapter":rs,"end_chapter":rs+remainder_count-1,"chapter_count":remainder_count,"reason":"new_partial_review_unit"}; scheduled+=remainder_count
        deferred=max(0,requested-scheduled)
        assignments=[]; manifest_rel=None
        if writer_blocks:
            run_root=production_run_relative(writer_start,writer_end); manifest_rel=run_root+"/manifest.json"; manifest_path=safe_workspace_path(root,manifest_rel,allow_missing=True); assignments=build_assignments(writer_start,writer_blocks,run_root=run_root)
            for sub in ("raw","reports"): safe_workspace_path(root,run_root+"/"+sub,allow_missing=True).mkdir(parents=True,exist_ok=True)
            manifest={"schema":2,"start_chapter":writer_start,"end_chapter":writer_end,"planned_chapters":writer_chapters,"parallel_chapters":writer_chapters,"writer_block_size":5,"writer_count":writer_blocks,"lookahead_blocks":lookahead,"created_at":utc_timestamp(),"assignments":assignments}
            atomic_write_json(manifest_path,manifest)
        nxt=dict(current)
        if writer_blocks:
            nxt["production_run"]={"start_chapter":writer_start,"end_chapter":writer_end,"writer_count":writer_blocks,"writer_block_size":5,"manifest":manifest_rel,"requested_start_chapter":next_new,"requested_end_chapter":next_new+requested-1,"scheduled_now_chapters":scheduled,"deferred_chapters":deferred,"lookahead_blocks":lookahead,"main_agent_prefix":prefix,"main_agent_remainder":remainder}
        else: nxt.pop("production_run",None)
        atomic_write_json(current_path,nxt)
    except ValueError as exc:p.error(str(exc))
    print(json.dumps({"prepared":True,"requested_new_chapters":requested,"planned_chapters":requested,"requested_range":f"{next_new}-{next_new+requested-1}","scheduled_now_chapters":scheduled,"deferred_chapters":deferred,"writer_count":writer_blocks,"writer_chapters":writer_chapters,"lookahead_blocks":lookahead,"main_agent_prefix":prefix,"main_agent_remainder":remainder,"manifest":manifest_rel,"assignments":assignments,"next":"先完成本 wave；每个前置块 final/commit 后，对后续 speculative block 做 rebase-production。若仍有 deferred_chapters，再准备下一 wave。"},ensure_ascii=False)); return 0
if __name__=="__main__":raise SystemExit(main())
