#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys

COMMANDS = {
    "init": "init_project",
    "configure": "configure_project",
    "context": "build_context",
    "search": "search_memory",
    "working-state": "working_state",
    "reader-model": "reader_model",
    "prepare-delta": "prepare_delta",
    "prepare-production": "prepare_production",
    "production-status": "production_status",
    "rebase-production": "rebase_production",
    "adopt-interface": "adopt_production_interface",
    "integration-metrics": "integration_metrics",
    "prepare-review": "prepare_batch_review",
    "finalize-review": "finalize_batch_review",
    "final-clean": "final_clean_check",
    "review-continuity": "review_continuity",
    "adjudicate-warning": "adjudicate_clean_warning",
    "review-rewrite": "review_rewrite",
    "ad-hoc-blind": "prepare_ad_hoc_blind",
    "adjudicate-interface": "adjudicate_production_interface",
    "chapter-stats": "chapter_stats",
    "commit": "commit_chapter",
    "entity-candidates": "entity_candidates",
    "recover": "recover_project",
    "seal-baselines": "seal_baselines",
    "impact": "rewrite_impact",
    "rewrite": "rewrite_prose",
    "confirm-rewrite": "confirm_rewrite",
    "audit": "audit_project",
    "quality": "quality_scan",
    "export": "export_novel",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help", "help"}:
        commands = "\n  ".join(sorted(COMMANDS))
        print("Novel Creator Flash helper\n\nUsage:\n  novelctl.py <command> [arguments]\n\nCommands:\n  " + commands)
        return 0
    command = sys.argv[1]
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"unknown command: {command}", file=sys.stderr)
        return 2
    module = importlib.import_module(module_name)
    old_argv = sys.argv
    forwarded = list(old_argv[2:])
    sys.argv = [f"{module_name}.py", *forwarded]
    try:
        return int(module.main())
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    raise SystemExit(main())
