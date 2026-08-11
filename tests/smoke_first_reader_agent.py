#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Opt-in Claude Code smoke test for loading and starting novel-fast-reader-flow."
    )
    parser.add_argument(
        "--require",
        action="store_true",
        help="Fail instead of skipping when the Claude Code CLI is missing or not authenticated.",
    )
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()

    claude = shutil.which("claude")
    if claude is None:
        message = {"smoke": "skipped", "reason": "Claude Code CLI is not installed"}
        print(json.dumps(message, ensure_ascii=False))
        return 1 if args.require else 0

    prompt = (
        "你是启动烟雾测试。不要调用任何工具。只输出一行：FIRST_READER_READY。"
    )
    env = {
        **os.environ,
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
    }
    try:
        proc = subprocess.run(
            [
                claude,
                "--agent",
                "novel-fast-reader-flow",
                "--print",
                "--max-turns",
                "1",
                "--no-session-persistence",
                prompt,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            encoding="utf-8",
            env=env,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"smoke": "failed", "reason": "timeout"}, ensure_ascii=False))
        return 1

    ready = proc.returncode == 0 and "FIRST_READER_READY" in proc.stdout
    result = {
        "smoke": "passed" if ready else "failed",
        "returncode": proc.returncode,
        "stdout": proc.stdout[-1000:],
        "stderr": proc.stderr[-1000:],
    }
    print(json.dumps(result, ensure_ascii=False))
    if ready:
        return 0
    # Missing login is a skip locally, but a required CI run must fail.
    return 1 if args.require else 0


if __name__ == "__main__":
    raise SystemExit(main())
