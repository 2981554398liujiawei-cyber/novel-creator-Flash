#!/usr/bin/env bash
set -euo pipefail
scope="project"
project_path=""
force="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope) scope="$2"; shift 2 ;;
    --project-path) project_path="$2"; shift 2 ;;
    --force) force="true"; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required")'
package_root="$(cd "$(dirname "$0")" && pwd)"
source_skill="$package_root/.claude/skills/novel-creator-fast-production"
source_agents="$package_root/.claude/agents"
agent_names=(
  novel-fast-writer-1.md novel-fast-writer-2.md novel-fast-writer-3.md novel-fast-writer-4.md novel-fast-writer-5.md
  novel-fast-reader-flow.md novel-fast-reader-character.md novel-fast-reader-hook.md
)
obsolete_agent_names=(novel-style-editor.md)
[[ -d "$source_skill" ]] || { echo "Skill source not found: $source_skill" >&2; exit 1; }
for name in "${agent_names[@]}"; do [[ -f "$source_agents/$name" ]] || { echo "Agent source not found: $name" >&2; exit 1; }; done
if find "$source_skill" "$source_agents" -type l -print -quit | grep -q .; then echo "Package source contains a symbolic link; refusing installation." >&2; exit 1; fi
if [[ "$scope" == "global" ]]; then claude_root="${HOME}/.claude";
elif [[ "$scope" == "project" ]]; then [[ -n "$project_path" ]] || project_path="$(pwd)"; claude_root="$(cd "$project_path" && pwd)/.claude";
else echo "scope must be project or global" >&2; exit 2; fi
skills_root="$claude_root/skills"; agents_root="$claude_root/agents"; mkdir -p "$skills_root" "$agents_root" "$claude_root/backups"
target_skill="$skills_root/novel-creator-fast-production"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"; token="$stamp-$$"
staging_skill="$skills_root/.novel-fast.install-$token"; staging_agents="$agents_root/.novel-fast-agents-install-$token"
backup_root="$claude_root/backups/novel-creator-fast-production-$token"
installed_skill="false"; installed_agents=(); moved_skill_backup="false"; moved_agent_backups=()
cleanup_staging(){ rm -rf "$staging_skill" "$staging_agents" 2>/dev/null || true; }
rollback(){ set +e; [[ "$installed_skill" == true ]] && rm -rf "$target_skill"; for n in "${installed_agents[@]}"; do rm -f "$agents_root/$n"; done; if [[ "$moved_skill_backup" == true && -d "$backup_root/skill" && ! -e "$target_skill" ]]; then mv "$backup_root/skill" "$target_skill"; fi; for n in "${moved_agent_backups[@]}"; do [[ -f "$backup_root/agents/$n" && ! -e "$agents_root/$n" ]] && mv "$backup_root/agents/$n" "$agents_root/$n"; done; cleanup_staging; }
trap rollback ERR; trap cleanup_staging EXIT
cp -RP "$source_skill" "$staging_skill"; mkdir -p "$staging_agents"; for n in "${agent_names[@]}"; do cp -P "$source_agents/$n" "$staging_agents/$n"; done
find "$staging_skill" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$staging_skill" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
python -m compileall -q "$staging_skill/scripts"
find "$staging_skill" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$staging_skill" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
conflicts=(); [[ -e "$target_skill" ]] && conflicts+=("$target_skill"); for n in "${agent_names[@]}"; do [[ -e "$agents_root/$n" ]] && conflicts+=("$agents_root/$n"); done
if (( ${#conflicts[@]} > 0 )) && [[ "$force" != true ]]; then printf 'Target exists; use --force to replace with backup:\n' >&2; printf '  %s\n' "${conflicts[@]}" >&2; exit 1; fi
need_backup=false; [[ -e "$target_skill" ]] && need_backup=true; for n in "${agent_names[@]}"; do [[ -e "$agents_root/$n" ]] && need_backup=true; done; for n in "${obsolete_agent_names[@]}"; do [[ -e "$agents_root/$n" ]] && need_backup=true; done
[[ "$need_backup" == true ]] && mkdir -p "$backup_root/agents"
if [[ -e "$target_skill" ]]; then mv "$target_skill" "$backup_root/skill"; moved_skill_backup=true; fi
for n in "${agent_names[@]}" "${obsolete_agent_names[@]}"; do if [[ -e "$agents_root/$n" ]]; then mv "$agents_root/$n" "$backup_root/agents/$n"; moved_agent_backups+=("$n"); fi; done
mv "$staging_skill" "$target_skill"; installed_skill=true
for n in "${agent_names[@]}"; do mv "$staging_agents/$n" "$agents_root/$n"; installed_agents+=("$n"); done
rmdir "$staging_agents"; trap - ERR; trap - EXIT
printf 'Installed novel-creator-fast-production Skill to %s\n' "$target_skill"
printf 'Installed %s rapid-production subagents to %s\n' "${#agent_names[@]}" "$agents_root"
if [[ "$moved_skill_backup" == true || ${#moved_agent_backups[@]} -gt 0 ]]; then printf 'Previous files backup: %s\n' "$backup_root"; fi
printf 'Restart Claude Code so the project or user subagents are loaded.\n'
