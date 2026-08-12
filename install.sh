#!/usr/bin/env bash
set -euo pipefail
scope="project"; project_path=""; positional_project_path=""; force="false"; migrate="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) [[ $# -ge 2 ]] || { echo "--scope requires a value" >&2; exit 2; }; scope="$2"; shift 2 ;;
    --project-path) [[ $# -ge 2 ]] || { echo "--project-path requires a value" >&2; exit 2; }; project_path="$2"; shift 2 ;;
    --force) force="true"; shift ;;
    --migrate) migrate="true"; shift ;;
    --*) echo "Unknown argument: $1" >&2; exit 2 ;;
    *) [[ -z "$positional_project_path" ]] || { echo "Only one positional project path is allowed" >&2; exit 2; }; positional_project_path="$1"; shift ;;
  esac
done
if [[ -n "$positional_project_path" ]]; then
  [[ "$scope" == "project" ]] || { echo "A positional project path cannot be used with --scope global" >&2; exit 2; }
  [[ -z "$project_path" || "$project_path" == "$positional_project_path" ]] || { echo "Positional project path conflicts with --project-path" >&2; exit 2; }
  project_path="$positional_project_path"
fi

python_cmd=()
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then python_cmd=("$candidate"); break; fi
done
if (( ${#python_cmd[@]} == 0 )) && command -v py >/dev/null 2>&1 && py -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then python_cmd=(py -3); fi
(( ${#python_cmd[@]} > 0 )) || { echo "Python 3.10+ is required (python3, python, or py -3)." >&2; exit 1; }

package_root="$(cd "$(dirname "$0")" && pwd)"; source_skill="$package_root/.claude/skills/novel-creator-flash"; source_agents="$package_root/.claude/agents"
agent_names=(novel-fast-writer-1.md novel-fast-writer-2.md novel-fast-writer-3.md novel-fast-writer-4.md novel-fast-writer-5.md novel-fast-writer-6.md novel-fast-writer-7.md novel-fast-writer-8.md novel-fast-writer-9.md novel-fast-writer-10.md novel-fast-reader-flow.md novel-fast-reader-character.md novel-fast-reader-hook.md novel-fast-continuity-reviewer.md); legacy_skill_names=(novel-creator-fast-production); legacy_agent_names=(novel-style-editor.md)
[[ -d "$source_skill" ]] || { echo "Skill source not found: $source_skill" >&2; exit 1; }
for n in "${agent_names[@]}"; do [[ -f "$source_agents/$n" ]] || { echo "Agent source not found: $n" >&2; exit 1; }; done
if find "$source_skill" "$source_agents" -type l -print -quit | grep -q .; then echo "Package source contains a symbolic link; refusing installation." >&2; exit 1; fi

if [[ "$scope" == "global" ]]; then claude_root="${HOME}/.claude";
elif [[ "$scope" == "project" ]]; then [[ -n "$project_path" ]] || project_path="$(pwd)"; claude_root="$(cd "$project_path" && pwd)/.claude";
else echo "scope must be project or global" >&2; exit 2; fi
skills_root="$claude_root/skills"; agents_root="$claude_root/agents"; backups_root="$claude_root/backups"; mkdir -p "$skills_root" "$agents_root" "$backups_root"
target_skill="$skills_root/novel-creator-flash"; stamp="$(date -u +%Y%m%dT%H%M%SZ)"; token="$stamp-$$"
staging_skill="$skills_root/.novel-creator-flash.install-$token"; staging_agents="$agents_root/.novel-creator-flash-agents-install-$token"; backup_root="$backups_root/novel-creator-flash-$token"

legacy_found=()
for n in "${legacy_skill_names[@]}"; do [[ -e "$skills_root/$n" ]] && legacy_found+=("skill:$n"); done
for n in "${legacy_agent_names[@]}"; do [[ -e "$agents_root/$n" ]] && legacy_found+=("agent:$n"); done
if (( ${#legacy_found[@]} > 0 )) && [[ "$migrate" != "true" ]]; then
  printf 'Legacy Novel Creator components detected (left untouched). Re-run with --migrate to back them up and remove them from active discovery:
' >&2
  printf '  %s
' "${legacy_found[@]}" >&2
fi

cleanup() { rm -rf "$staging_skill" "$staging_agents" 2>/dev/null || true; }
trap cleanup EXIT
cp -RP "$source_skill" "$staging_skill"; mkdir -p "$staging_agents"; for n in "${agent_names[@]}"; do cp -P "$source_agents/$n" "$staging_agents/$n"; done
find "$staging_skill" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true; find "$staging_skill" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
"${python_cmd[@]}" -m compileall -q "$staging_skill/scripts"
find "$staging_skill" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true; find "$staging_skill" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

conflicts=(); [[ -e "$target_skill" ]] && conflicts+=("$target_skill"); for n in "${agent_names[@]}"; do [[ -e "$agents_root/$n" ]] && conflicts+=("$agents_root/$n"); done
if (( ${#conflicts[@]} > 0 )) && [[ "$force" != "true" ]]; then printf 'Target exists; use --force to replace with backup:
' >&2; printf '  %s
' "${conflicts[@]}" >&2; exit 1; fi
mkdir -p "$backup_root/agents" "$backup_root/legacy-skills" "$backup_root/legacy-agents"
if [[ -e "$target_skill" ]]; then mv "$target_skill" "$backup_root/skill"; fi
for n in "${agent_names[@]}"; do [[ -e "$agents_root/$n" ]] && mv "$agents_root/$n" "$backup_root/agents/$n"; done
mv "$staging_skill" "$target_skill"; for n in "${agent_names[@]}"; do mv "$staging_agents/$n" "$agents_root/$n"; done; rmdir "$staging_agents"

if [[ "$migrate" == "true" ]]; then
  for n in "${legacy_skill_names[@]}"; do [[ -e "$skills_root/$n" ]] && mv "$skills_root/$n" "$backup_root/legacy-skills/$n"; done
  for n in "${legacy_agent_names[@]}"; do [[ -e "$agents_root/$n" ]] && mv "$agents_root/$n" "$backup_root/legacy-agents/$n"; done
fi
trap - EXIT
printf 'Installed novel-creator-flash Skill to %s
' "$target_skill"; printf 'Installed %s subagents to %s
' "${#agent_names[@]}" "$agents_root"
[[ "$migrate" == "true" && ${#legacy_found[@]} -gt 0 ]] && printf 'Legacy components moved to backup: %s
' "$backup_root"
printf 'Python launcher validated with: %s
' "${python_cmd[*]}"; printf 'Restart Claude Code so project/user subagents are reloaded.
'
