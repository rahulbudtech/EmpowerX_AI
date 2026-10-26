#!/bin/bash
# Syntax and policy checks that do not need the production server.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$ROOT/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

fail=0
while IFS= read -r script; do
  if ! bash -n "$script"; then
    log "syntax error in $script"
    fail=1
  fi
done <<EOF
$(find "$ROOT" -type f \( -name '*.sh' -o -name 'empowerx-ai-*' \) | sort)
EOF

py="${EMPOWERX_AI_PYTHON:-python3}"
if ! "$py" "$ROOT/lib/scan_migrations.py" "$REPO/migrations"; then
  fail=1
fi
for file in "$ROOT"/lib/*.py; do
  "$py" -m py_compile "$file"
done

if grep -R -n -E \
  --exclude 'run-static-checks.sh' \
  --exclude '*.pyc' \
  'pm2 (restart|reload|delete) all|pm2 kill|git reset --hard|git clean|alembic downgrade|pg_restore' \
  "$ROOT" "$REPO/.github"; then
  log "forbidden command present"
  fail=1
fi

if grep -q "requiretty" "$ROOT/vps/sudoers-empowerx-ai-deploy"; then
  log "sudoers contains unsupported requiretty"
  fail=1
fi

[[ "$fail" -eq 0 ]] || die 1 "static checks failed"
log "static checks passed"
