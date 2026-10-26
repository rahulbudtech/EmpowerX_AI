#!/bin/bash
# Build a checksummed release bundle from a git commit.
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/lib/common.sh"

SHA_INPUT="${1:-HEAD}"
OUT="${2:-}"
REPO="${EMPOWERX_AI_REPO:-$(cd "$ROOT/.." && pwd)}"

if ! git -C "$REPO" cat-file -e "${SHA_INPUT}^{commit}" 2>/dev/null; then
  die 2 "invalid commit sha"
fi
SHA="$(git -C "$REPO" rev-parse "${SHA_INPUT}^{commit}")"
assert_sha "$SHA" || die 2 "invalid commit sha"

if [[ -z "$OUT" ]]; then
  OUT="$REPO/dist/deploy/empowerx-ai-$SHA.tar.gz"
fi
mkdir -p "$(dirname "$OUT")"

if [[ -n "${GITHUB_REF:-}" ]]; then
  case "$GITHUB_REF" in
    refs/heads/bud_development) BRANCH="bud_development" ;;
    *) BRANCH="${GITHUB_REF#refs/heads/}" ;;
  esac
elif git -C "$REPO" symbolic-ref -q HEAD >/dev/null 2>&1; then
  BRANCH="$(git -C "$REPO" symbolic-ref --short HEAD)"
else
  BRANCH="detached"
fi

workdir="$(mktemp -d)"
cleanup() { rm -rf "$workdir"; }
trap cleanup EXIT

git -C "$REPO" archive --format=tar.gz -o "$workdir/source.tar.gz" "$SHA"
inner="$(sha256_file "$workdir/source.tar.gz")"
created="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$workdir/manifest.txt" <<EOF
commit=$SHA
archive_sha256=$inner
branch=$BRANCH
created_at=$created
EOF
# COPYFILE_DISABLE stops macOS tar from inserting AppleDouble members.
COPYFILE_DISABLE=1 tar -C "$workdir" -czf "$OUT" manifest.txt source.tar.gz
outer="$(sha256_file "$OUT")"
printf '%s\n' "$outer" > "${OUT}.sha256"
printf '%s\n' "$SHA" > "${OUT}.commit"

py="$(python_bin)"
verify="$(mktemp -d)"
if ! "$py" "$ROOT/lib/validate_archive.py" deploy-bundle \
  --archive "$OUT" \
  --checksum "$outer" \
  --expected-commit "$SHA" \
  --dest "$verify/tree" \
  --require-branch "$BRANCH"; then
  rm -f "$OUT" "${OUT}.sha256" "${OUT}.commit"
  rm -rf "$verify"
  die 3 "packaged artifact failed validation"
fi
rm -rf "$verify"
log "packaged $SHA"
printf '%s\n' "$OUT"
