#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from novelctl import COMMANDS


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        commands = "\n  ".join(sorted(COMMANDS))
        print("Novel Creator Skill-bound helper\n\nUsage:\n  novelctl-skill <command> [arguments]\n\nThe workspace is always the current Claude Code project directory and cannot be overridden.\n\nCommands:\n  " + commands)
        return 0

    command = sys.argv[1]
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"unknown command: {command}", file=sys.stderr)
        return 2

    root = Path.cwd().resolve()
    if not root.is_dir():
        print("current Claude project directory is unavailable", file=sys.stderr)
        return 2

    module = importlib.import_module(module_name)
    old_argv = sys.argv
    # Inject exactly one workspace positional argument. Any caller-supplied extra
    # positional path remains after the injected workspace and argparse rejects it.
    sys.argv = [f"{module_name}.py", str(root), *old_argv[2:]]
    try:
        return int(module.main())
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    raise SystemExit(main())
