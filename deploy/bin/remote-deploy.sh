#!/bin/bash
# Upload a bundle and run the restricted remote deploy command.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

SHA="${1:-${GITHUB_SHA:-}}"
assert_sha "$SHA" || die 2 "invalid commit sha"
[[ -n "${VPS_HOST:-}" ]] || die 4 "missing VPS_HOST"
[[ -n "${VPS_USER:-}" ]] || die 4 "missing VPS_USER"
[[ -n "${VPS_PORT:-}" ]] || die 4 "missing VPS_PORT"
case "$VPS_PORT" in
  *[!0-9]*|"") die 4 "invalid VPS_PORT" ;;
esac
[[ -n "${EMPOWERX_AI_SSH_IDENTITY:-}" && -f "$EMPOWERX_AI_SSH_IDENTITY" ]] || die 4 "missing SSH identity"
[[ -n "${EMPOWERX_AI_KNOWN_HOSTS:-}" && -f "$EMPOWERX_AI_KNOWN_HOSTS" ]] || die 4 "missing known hosts"

if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  REPO="$(cd "$ROOT/.." && pwd)"
else
  REPO="${EMPOWERX_AI_REPO:-$(cd "$ROOT/.." && pwd)}"
fi

SSH_BIN="${EMPOWERX_AI_SSH:-ssh}"
artifact="${EMPOWERX_AI_ARTIFACT:-$REPO/dist/deploy/empowerx-ai-$SHA.tar.gz}"
if [[ "${EMPOWERX_AI_SKIP_PACKAGE:-}" != "1" ]]; then
  EMPOWERX_AI_REPO="$REPO" "$ROOT/bin/package-artifact.sh" "$SHA" "$artifact" >/dev/null
fi
[[ -f "$artifact" ]] || die 3 "missing packaged artifact"
sum="$(sha256_file "$artifact")"

ssh_exec() {
  "$SSH_BIN" -T \
    -i "$EMPOWERX_AI_SSH_IDENTITY" \
    -p "$VPS_PORT" \
    -o BatchMode=yes \
    -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=$EMPOWERX_AI_KNOWN_HOSTS" \
    -o ConnectTimeout=20 \
    "$VPS_USER@$VPS_HOST" \
    "$@"
}

log "uploading $SHA"
{
  printf '%s\n' "$sum"
  cat "$artifact"
} | ssh_exec "receive-artifact $SHA"

if [[ "${EMPOWERX_AI_ALLOW_MIGRATIONS:-}" == "yes" ]]; then
  log "remote deploy $SHA with migration approval flag"
  ssh_exec "deploy $SHA migrate=yes"
else
  log "remote deploy $SHA"
  ssh_exec "deploy $SHA"
fi
