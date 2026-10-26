#!/bin/bash
# Read a checksum line plus a gzip bundle from stdin.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

SHA="${1:-}"
assert_sha "$SHA" || die 2 "invalid commit sha"
load_config
mkdir -p "$EMPOWERX_AI_STATE_DIR/incoming" "$EMPOWERX_AI_STATE_DIR/logs" "$EMPOWERX_AI_STATE_DIR/locks"
acquire_lock
trap 'release_lock' EXIT

expected=""
IFS= read -r expected || die 3 "missing checksum"
case "$expected" in
  *[!0-9a-f]*|"") die 3 "malformed checksum" ;;
esac
[[ "${#expected}" -eq 64 ]] || die 3 "malformed checksum"

tmp="$(mktemp "$EMPOWERX_AI_STATE_DIR/incoming/.partial.XXXXXX")"
cleanup() {
  if [[ -n "${tmp:-}" && -f "$tmp" ]]; then
    rm -f "$tmp"
  fi
}
trap 'cleanup; release_lock' EXIT

max_bytes=$((250 * 1024 * 1024))
# One extra megabyte so an oversized stream can be detected and discarded.
dd if=/dev/stdin of="$tmp" bs=1048576 count=251 status=none
size="$(wc -c < "$tmp" | tr -d ' ')"
if [[ "$size" -gt "$max_bytes" ]]; then
  die 3 "artifact exceeds size limit"
fi
if [[ "$size" -lt 32 ]]; then
  die 3 "artifact is too small"
fi

magic="$(od -An -t x1 -N 2 "$tmp" | tr -d ' \n')"
[[ "$magic" == "1f8b" ]] || die 3 "artifact is not gzip"

actual="$(sha256_file "$tmp")"
[[ "$actual" == "$expected" ]] || die 3 "checksum mismatch"

install -m 600 "$tmp" "$EMPOWERX_AI_STATE_DIR/incoming/$SHA.tar.gz"
printf '%s\n' "$expected" > "$EMPOWERX_AI_STATE_DIR/incoming/$SHA.sha256"
chmod 600 "$EMPOWERX_AI_STATE_DIR/incoming/$SHA.sha256"
rm -f "$tmp"
tmp=""
log "received artifact $SHA"
