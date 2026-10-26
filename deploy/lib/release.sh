#!/bin/bash
# Release build and activation. Live persistent data is never replaced.

if ! declare -f log >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
fi

DEPLOY_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mark_mutated() {
  mkdir -p "$EMPOWERX_AI_STATE_DIR/state"
  printf '1\n' > "$EMPOWERX_AI_STATE_DIR/state/live-mutated"
}

write_catalog() {
  local sha="$1" code="$2" venv="$3"
  mkdir -p "$EMPOWERX_AI_STATE_DIR/catalog"
  cat > "$EMPOWERX_AI_STATE_DIR/catalog/$sha" <<EOF
code=$code
venv=$venv
EOF
}

catalog_venv() {
  local sha="$1" file value
  file="$EMPOWERX_AI_STATE_DIR/catalog/$sha"
  [[ -f "$file" ]] || return 1
  value="$(awk -F= '/^venv=/{print substr($0, index($0, "=") + 1)}' "$file")"
  [[ -n "$value" ]] || return 1
  printf '%s\n' "$value"
}

atomic_symlink() {
  local target="$1" linkpath="$2" py
  py="$(python_bin)"
  "$py" "$DEPLOY_LIB/atomic_symlink.py" "$target" "$linkpath"
}

assert_persistent() {
  local name path
  for name in .env uploads preprocessed; do
    path="$EMPOWERX_AI_LIVE_DIR/$name"
    if [[ -L "$path" ]]; then
      die 4 "refusing to deploy because $name is a symlink"
    fi
    if [[ ! -e "$path" ]]; then
      die 4 "missing persistent path $name"
    fi
  done
  if production_mode; then
    "$EMPOWERX_AI_PYTHON" - "$EMPOWERX_AI_LIVE_DIR/.env" <<'PY'
import os, stat, sys
mode = os.stat(sys.argv[1]).st_mode
if mode & (stat.S_IROTH | stat.S_IWOTH):
    sys.stderr.write("production env file is world-accessible\n")
    raise SystemExit(1)
PY
  fi
}

safe_rm_release() {
  local dest="$1" base baseline
  base="$(basename "$dest")"
  assert_sha "$base" || return 1
  [[ "$dest" == "$EMPOWERX_AI_STATE_DIR/releases/$base" ]] || return 1
  [[ -L "$dest" ]] && return 1
  if [[ -f "$EMPOWERX_AI_STATE_DIR/state/baseline" ]]; then
    baseline="$(read_state baseline || true)"
    [[ "$base" == "$baseline" ]] && return 1
  fi
  rm -rf "$dest"
}

build_release() {
  local release="$1" py uv
  py="$EMPOWERX_AI_PYTHON"
  uv="$EMPOWERX_AI_UV"
  [[ -x "$py" ]] || {
    log "managed Python 3.12 is not executable"
    return 5
  }
  case "$py" in
    /usr/bin/python*|/bin/python*)
      log "refusing the operating-system Python"
      return 5
      ;;
  esac
  if ! "$py" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
    log "refusing an interpreter that is not Python 3.12"
    return 5
  fi
  if [[ -e "$release/.venv" ]]; then
    log "release virtualenv already exists"
    return 5
  fi
  if ! "$py" -m venv "$release/.venv"; then
    log "virtualenv creation failed"
    return 5
  fi
  if ! "$uv" pip install --python "$release/.venv/bin/python" -r "$release/requirements.txt"; then
    log "dependency installation failed"
    return 5
  fi
  if ! (cd "$release" && "$release/.venv/bin/python" -c 'import app.main, app.workers.celery_app'); then
    log "application import failed after dependency installation"
    return 5
  fi
  printf 'ok\n' > "$release/.release-complete"
  return 0
}

preserve_live_venv() {
  local baseline preserved live_venv
  baseline="$(read_state baseline || true)"
  [[ -n "$baseline" ]] || return 0
  preserved="$EMPOWERX_AI_STATE_DIR/venvs/baseline"
  live_venv="$EMPOWERX_AI_LIVE_DIR/.venv"
  if [[ -x "$preserved/bin/python" ]]; then
    write_catalog "$baseline" "$EMPOWERX_AI_STATE_DIR/releases/$baseline" "$preserved"
    return 0
  fi
  if [[ -L "$live_venv" ]]; then
    return 0
  fi
  if [[ -d "$live_venv" ]]; then
    mkdir -p "$EMPOWERX_AI_STATE_DIR/venvs"
    mark_mutated
    mv "$live_venv" "$preserved"
    atomic_symlink "$preserved" "$live_venv" || return 1
    write_catalog "$baseline" "$EMPOWERX_AI_STATE_DIR/releases/$baseline" "$preserved"
  fi
  return 0
}

link_code_entry() {
  local name="$1" target="$2" dest backup stamp
  case "$name" in
    .env|uploads|preprocessed|.venv|.git|.release-complete) return 0 ;;
  esac
  dest="$EMPOWERX_AI_LIVE_DIR/$name"
  if [[ -L "$dest" ]]; then
    mark_mutated
    atomic_symlink "$target" "$dest"
    return
  fi
  if [[ -e "$dest" ]]; then
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup="$EMPOWERX_AI_STATE_DIR/backups/replaced/$stamp/$name"
    mkdir -p "$(dirname "$backup")"
    mark_mutated
    mv "$dest" "$backup"
  else
    mark_mutated
  fi
  atomic_symlink "$target" "$dest"
}

selected_venv() {
  local sha="$1" release venv
  release="$EMPOWERX_AI_STATE_DIR/releases/$sha"
  if [[ -x "$release/.venv/bin/python" ]]; then
    printf '%s\n' "$release/.venv"
    return 0
  fi
  venv="$(catalog_venv "$sha" || true)"
  [[ -n "$venv" && -x "$venv/bin/python" ]] || return 1
  printf '%s\n' "$venv"
}

remove_stale_links() {
  local release="$1" existing name target
  for existing in "$EMPOWERX_AI_LIVE_DIR"/* "$EMPOWERX_AI_LIVE_DIR"/.[!.]*; do
    [[ -L "$existing" ]] || continue
    name="$(basename "$existing")"
    case "$name" in
      .env|uploads|preprocessed|.venv) continue ;;
    esac
    [[ -e "$release/$name" ]] && continue
    target="$(readlink "$existing" || true)"
    case "$target" in
      "$EMPOWERX_AI_STATE_DIR/releases/"*)
        mark_mutated
        rm -f "$existing"
        ;;
    esac
  done
}

switch_release() {
  local sha="$1" release name path venv
  assert_sha "$sha" || die 2 "invalid commit sha"
  release="$EMPOWERX_AI_STATE_DIR/releases/$sha"
  [[ -d "$release/app" ]] || die 7 "release is missing application code"
  [[ -f "$release/.release-complete" ]] || die 7 "release is not built"
  case "$release" in
    "$EMPOWERX_AI_STATE_DIR/releases/$sha") ;;
    *) die 7 "refusing release path" ;;
  esac
  if [[ -L "$release" ]]; then
    die 7 "refusing symlinked release directory"
  fi
  assert_persistent
  preserve_live_venv || die 7 "could not preserve the existing virtualenv"
  for path in "$release"/* "$release"/.[!.]*; do
    [[ -e "$path" ]] || continue
    name="$(basename "$path")"
    link_code_entry "$name" "$path" || die 7 "could not activate $name"
  done
  remove_stale_links "$release"
  venv="$(selected_venv "$sha" || true)"
  [[ -n "$venv" ]] || die 7 "release virtualenv is missing"
  if [[ -e "$EMPOWERX_AI_LIVE_DIR/.venv" && ! -L "$EMPOWERX_AI_LIVE_DIR/.venv" ]]; then
    die 7 "live virtualenv is still a real directory"
  fi
  mark_mutated
  atomic_symlink "$venv" "$EMPOWERX_AI_LIVE_DIR/.venv" || die 7 "could not switch virtualenv"
  write_catalog "$sha" "$release" "$venv"
  log "activated release $sha"
}

prune_releases() {
  local keep_file path sha count=0
  keep_file="$(mktemp)"
  {
    [[ -f "$EMPOWERX_AI_STATE_DIR/state/current" ]] && cat "$EMPOWERX_AI_STATE_DIR/state/current"
    [[ -f "$EMPOWERX_AI_STATE_DIR/state/previous" ]] && cat "$EMPOWERX_AI_STATE_DIR/state/previous"
    [[ -f "$EMPOWERX_AI_STATE_DIR/state/baseline" ]] && cat "$EMPOWERX_AI_STATE_DIR/state/baseline"
  } | awk 'NF' | sort -u > "$keep_file"
  for path in $(ls -1dt "$EMPOWERX_AI_STATE_DIR/releases"/*/ 2>/dev/null || true); do
    sha="$(basename "$path")"
    assert_sha "$sha" || continue
    if grep -qx "$sha" "$keep_file"; then
      continue
    fi
    count=$((count + 1))
    if [[ "$count" -le 5 ]]; then
      continue
    fi
    if [[ -L "$EMPOWERX_AI_STATE_DIR/releases/$sha" ]]; then
      continue
    fi
    rm -rf "$EMPOWERX_AI_STATE_DIR/releases/$sha"
    log "pruned old release $sha"
  done
  rm -f "$keep_file"
}

snapshot_code() {
  local src="$1" dest="$2"
  mkdir -p "$dest"
  rsync -a \
    --exclude '.env' \
    --exclude '.env.local' \
    --exclude 'uploads' \
    --exclude 'preprocessed' \
    --exclude '.venv' \
    --exclude '.git' \
    "$src"/ "$dest"/
}
