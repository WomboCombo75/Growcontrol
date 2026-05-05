#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/pi/Growcontrol}"
cd "$PROJECT_DIR"

# Determine current branch + upstream if configured.
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"

# Fallback upstream guess.
if [[ -z "$upstream" ]]; then
  if git show-ref --verify --quiet refs/remotes/origin/main; then
    upstream="origin/main"
  elif git show-ref --verify --quiet refs/remotes/origin/master; then
    upstream="origin/master"
  fi
fi

checked_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

out_dir="$(python3 - <<'PY'
import json, pathlib
settings = pathlib.Path("config/settings.json")
try:
    s = json.loads(settings.read_text(encoding="utf-8"))
except Exception:
    s = {}
print(s.get("output_dir", "/var/www/html/growcontrol"))
PY
)"

mkdir -p "$out_dir"
out_file="$out_dir/update_status.json"

write_json() {
  python3 - "$out_file" <<'PY' "$@"
import json, sys
path = sys.argv[1]
payload = json.loads(sys.stdin.read())
with open(path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
PY
}

if ! command -v git >/dev/null 2>&1; then
  write_json <<EOF
{"checked_at":"$checked_at","update_available":null,"behind_count":null,"branch":$([[ -n "$branch" ]] && printf '"%s"' "$branch" || printf 'null'),"upstream":null,"error":"git not found"}
EOF
  exit 0
fi

if [[ -z "$upstream" ]]; then
  write_json <<EOF
{"checked_at":"$checked_at","update_available":null,"behind_count":null,"branch":$([[ -n "$branch" ]] && printf '"%s"' "$branch" || printf 'null'),"upstream":null,"error":"no upstream configured (set tracking branch or origin/main)"}
EOF
  exit 0
fi

err=""
if ! git fetch --quiet --prune 2>/dev/null; then
  err="git fetch failed"
fi

behind=""
if [[ -z "$err" ]]; then
  behind="$(git rev-list --count "HEAD..$upstream" 2>/dev/null || echo "")"
  [[ "$behind" =~ ^[0-9]+$ ]] || { err="failed to compute behind count"; behind=""; }
fi

update_available="null"
behind_count="null"
if [[ -z "$err" ]]; then
  behind_count="$behind"
  if [[ "$behind" -gt 0 ]]; then
    update_available="true"
  else
    update_available="false"
  fi
fi

write_json <<EOF
{
  "checked_at": "$checked_at",
  "update_available": $update_available,
  "behind_count": ${behind_count},
  "branch": $([[ -n "$branch" ]] && printf '"%s"' "$branch" || printf 'null'),
  "upstream": "$(printf "%s" "$upstream")",
  "error": $([[ -n "$err" ]] && printf '"%s"' "$err" || printf 'null')
}
EOF

