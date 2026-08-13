#!/usr/bin/env python3
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from novelctl import COMMANDS


def _project_root() -> Path:
    env_root = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if env_root:
        root = Path(env_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("CLAUDE_PROJECT_DIR is not a directory")
        return root

    # Project-scope installers write a private binding marker into the installed
    # skill. This fallback is immutable from caller arguments and independent of cwd.
    skill_root = Path(__file__).resolve(strict=True).parents[1]
    marker = skill_root / ".project-root"
    if marker.is_file():
        raw = marker.read_text(encoding="utf-8-sig").strip()
        if not raw:
            raise ValueError("project-root binding marker is empty")
        root = Path(raw).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("project-root binding marker points to a missing directory")
        expected = root / ".claude" / "skills" / skill_root.name
        if expected.resolve(strict=True) != skill_root.resolve(strict=True):
            raise ValueError("project-root binding marker does not match this installed skill")
        return root

    raise ValueError(
        "Skill-bound CLI cannot determine the Claude project root. "
        "Use a project-scope install or run under Claude Code with CLAUDE_PROJECT_DIR available."
    )


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        commands = "\n  ".join(sorted(COMMANDS))
        print(
            "Novel Creator Skill-bound helper\n\n"
            "Usage:\n  novelctl-skill <command> [arguments]\n\n"
            "The workspace is bound to the Claude Code project root and cannot be overridden.\n\n"
            "Commands:\n  " + commands
        )
        return 0

    command = sys.argv[1]
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"unknown command: {command}", file=sys.stderr)
        return 2

    try:
        root = _project_root()
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    module = importlib.import_module(module_name)
    old_argv = sys.argv
    # Inject the one trusted workspace positional. A caller-supplied path remains an
    # extra positional argument and argparse rejects it; changing Bash cwd has no effect.
    sys.argv = [f"{module_name}.py", str(root), *old_argv[2:]]
    try:
        return int(module.main())
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    raise SystemExit(main())
