#!/usr/bin/env python3
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
SUITES=("context","literary","continuity")
if __name__ == "__main__":
    start=time.perf_counter(); failures=[]; runner=Path(__file__).with_name("run_tests.py")
    for suite in SUITES:
        print(f"[SUITE] {suite}", flush=True)
        if subprocess.run([sys.executable,str(runner),"--suite",suite]).returncode:
            failures.append(suite)
    print(json.dumps({"suites":list(SUITES),"failures":failures,"seconds":round(time.perf_counter()-start,2)},ensure_ascii=False))
    raise SystemExit(1 if failures else 0)
