#!/bin/bash
# One-time: install the deploy scripts from an already-uploaded artifact.
# Does not touch .env, uploads, the database, live release symlinks, or PM2.
set -euo pipefail

SHA="${1:-}"
STATE_DIR="${EMPOWERX_AI_STATE_DIR:-/var/lib/empowerx-ai}"

if [[ "$(id -u)" -ne 0 ]]; then
  printf '%s\n' "ERROR: run as root" >&2
  exit 1
fi
if [[ "${#SHA}" -ne 40 ]]; then
  printf '%s\n' "usage: bootstrap-from-incoming.sh SHA" >&2
  exit 2
fi
case "$SHA" in
  *[!0-9a-f]*)
    printf '%s\n' "usage: bootstrap-from-incoming.sh SHA" >&2
    exit 2
    ;;
esac

incoming="$STATE_DIR/incoming/${SHA}.tar.gz"
if [[ ! -f "$incoming" ]]; then
  printf '%s\n' "ERROR: missing incoming artifact $incoming" >&2
  exit 4
fi

work="$(mktemp -d /tmp/empowerx-ai-bootstrap.XXXXXX)"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

tar -C "$work" -xzf "$incoming"
mkdir -p "$work/src"
tar -C "$work/src" -xzf "$work/source.tar.gz"
[[ -f "$work/src/deploy/vps/install.sh" ]] || {
  printf '%s\n' "ERROR: incoming artifact is missing deploy scripts" >&2
  exit 4
}

bash "$work/src/deploy/vps/install.sh" --scripts-only --bundle "$work/src/deploy"
printf '%s\n' "Installed deploy scripts now accept bud_staging."
printf '%s\n' "Re-run the GitHub Actions deploy job, or:"
printf '%s\n' "  sudo -u empowerx-ai-deploy /usr/local/lib/empowerx-ai/deploy/bin/deploy.sh $SHA"
