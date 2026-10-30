#!/bin/bash
# Copy the current application tree once, without secrets or the database.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"
# shellcheck disable=SC1091
source "$ROOT/lib/release.sh"

load_config
if production_mode && [[ "$(id -u)" -ne 0 ]]; then
  die 4 "baseline backup must run as root"
fi

SHA="${EMPOWERX_AI_BASELINE_SHA:-}"
assert_sha "$SHA" || die 2 "invalid baseline sha"
mkdir -p "$EMPOWERX_AI_STATE_DIR/backups" "$EMPOWERX_AI_STATE_DIR/releases" "$EMPOWERX_AI_STATE_DIR/state"

dest_backup="$EMPOWERX_AI_STATE_DIR/backups/baseline-$SHA"
dest_release="$EMPOWERX_AI_STATE_DIR/releases/$SHA"
if [[ -d "$dest_backup" && -f "$dest_release/.release-complete" ]]; then
  log "baseline backup already present for $SHA"
  write_state baseline "$SHA"
  exit 0
fi

snapshot_code "$EMPOWERX_AI_LIVE_DIR" "$dest_backup"
if [[ ! -d "$dest_release/app" ]]; then
  snapshot_code "$EMPOWERX_AI_LIVE_DIR" "$dest_release"
fi
printf 'ok\n' > "$dest_release/.release-complete"
cat > "$dest_backup/MANIFEST" <<EOF
baseline_sha=$SHA
created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
live_dir=$EMPOWERX_AI_LIVE_DIR
note=code only; env uploads preprocessed virtualenv and database are excluded
EOF
write_state baseline "$SHA"
if [[ ! -f "$EMPOWERX_AI_STATE_DIR/state/current" ]]; then
  write_state current "$SHA"
fi
log "baseline backup recorded for $SHA"
