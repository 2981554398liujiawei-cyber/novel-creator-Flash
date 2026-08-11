#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ALL_SUITES = (
    "core",
    "context",
    "literary",
    "continuity",
    "transactions",
    "security",
    "rewrite-install",
    "benchmark",
)
DEFAULT_SUITE_TIMEOUT_SECONDS = 300


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Novel Creator functional suites without outer parallelism.")
    parser.add_argument(
        "--suites",
        nargs="+",
        choices=ALL_SUITES,
        default=list(ALL_SUITES),
        help="Optional subset for CI sharding; default runs every suite in order.",
    )
    parser.add_argument("--suite-timeout", type=int, default=DEFAULT_SUITE_TIMEOUT_SECONDS)
    args = parser.parse_args()

    runner = Path(__file__).resolve().with_name("run_tests.py")
    started = time.perf_counter()
    failures: list[str] = []
    durations: dict[str, float] = {}
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }

    # Run one suite process at a time. The tests themselves already invoke many
    # short-lived CLI subprocesses; outer parallelism made full runs slower and
    # less predictable on Windows and resource-constrained CI workers.
    for suite in args.suites:
        print(f"\n=== Novel Creator suite: {suite} ===", flush=True)
        suite_started = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, str(runner), "--suite", suite],
                cwd=runner.parents[1],
                env=env,
                timeout=args.suite_timeout,
            )
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            ok = False
            print(f"[TIMEOUT] {suite} exceeded {args.suite_timeout}s", file=sys.stderr, flush=True)
        durations[suite] = round(time.perf_counter() - suite_started, 2)
        if not ok:
            failures.append(suite)

    summary = {
        "suites": list(args.suites),
        "failures": failures,
        "durations": durations,
        "seconds": round(time.perf_counter() - started, 2),
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
