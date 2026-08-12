#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, os, shutil, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
AGENT='novel-fast-reader-flow'
MARKER="FIRST_READER_BACKGROUND_READY"

def main()->int:
    parser=argparse.ArgumentParser(description="Opt-in real Claude Code background-subagent smoke test.")
    parser.add_argument("--require",action="store_true")
    parser.add_argument("--timeout",type=int,default=150)
    args=parser.parse_args(); claude=shutil.which("claude")
    if not claude:
        print(json.dumps({"smoke":"skipped","reason":"Claude Code CLI is not installed"},ensure_ascii=False)); return 1 if args.require else 0
    with tempfile.TemporaryDirectory(prefix="reader-bg-smoke-") as tmp:
        project=Path(tmp)
        install=subprocess.run(["bash",str(ROOT/"install.sh"),str(project)],cwd=ROOT,text=True,capture_output=True,timeout=90)
        if install.returncode!=0:
            print(json.dumps({"smoke":"failed","reason":"install failed","stderr":install.stderr[-1200:]},ensure_ascii=False)); return 1
        packet=project/".novel"/"blind-packets"/"smoke.md"
        packet.parent.mkdir(parents=True,exist_ok=True)
        packet.write_text("# Blind packet smoke\n\n测试正文。\n",encoding="utf-8")
        prompt=(f"必须使用 Agent 工具调用 {AGENT} 作为后台 subagent。任务消息只给它精确路径 .novel/blind-packets/smoke.md。"
                f"要求该 subagent 只用 Read 读取这个 blind packet，然后返回 {MARKER}。等待后台结果后，你自己只输出 {MARKER}。")
        env={**os.environ,"CLAUDE_CODE_SKIP_PROMPT_HISTORY":"1"}
        try:
            proc=subprocess.run([claude,"--print","--permission-mode","dontAsk","--allowedTools","Agent", "--no-session-persistence",prompt],cwd=project,text=True,capture_output=True,encoding="utf-8",env=env,timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print(json.dumps({"smoke":"failed","reason":"timeout"},ensure_ascii=False)); return 1
        combined=(proc.stdout or "")+"\n"+(proc.stderr or "")
        ready=proc.returncode==0 and MARKER in combined
        print(json.dumps({"smoke":"passed" if ready else "failed","returncode":proc.returncode,"stdout":proc.stdout[-1200:],"stderr":proc.stderr[-1200:]},ensure_ascii=False))
        return 0 if ready else (1 if args.require else 0)
if __name__=="__main__": raise SystemExit(main())
