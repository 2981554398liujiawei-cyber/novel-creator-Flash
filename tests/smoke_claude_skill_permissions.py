#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, shutil, subprocess, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; SKILL_NAME='novel-creator-flash'; EXPECTED='Novel Creator Skill-bound helper'
def main()->int:
    p=argparse.ArgumentParser(description="Real Claude Code smoke test for Skill launcher pre-approval."); p.add_argument("--require",action="store_true"); args=p.parse_args(); claude=shutil.which("claude")
    if not claude:
        print("SKIP: claude CLI is not installed; real Skill permission smoke was not executed."); return 2 if args.require else 0
    with tempfile.TemporaryDirectory(prefix=SKILL_NAME+"-smoke-") as tmp:
        project=Path(tmp); install=subprocess.run(["bash",str(ROOT/"install.sh"),str(project)],cwd=ROOT,text=True,capture_output=True,timeout=90)
        if install.returncode!=0: print(install.stdout); print(install.stderr); return 1
        prompt=(f"/{SKILL_NAME} 权限烟雾测试：必须实际调用 Bash，并且只执行该 Skill 自带的 scripts/novelctl-skill launcher 加 --help。不要修改任何项目文件；最后报告命令输出第一行。")
        proc=subprocess.run([claude,"--print","--permission-mode","dontAsk",prompt],cwd=project,text=True,capture_output=True,timeout=180,env=os.environ.copy())
        combined=(proc.stdout or "")+"\n"+(proc.stderr or "")
        if proc.returncode!=0 or EXPECTED not in combined: print(combined); print("FAIL: launcher pre-approval smoke failed."); return 1
        print("PASS: real Claude Code Skill launcher permission smoke succeeded."); return 0
if __name__=="__main__": raise SystemExit(main())
