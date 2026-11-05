#!/bin/bash
# One-time installer for production AI deploys. Does not restart PM2 or databases.
set -euo pipefail

ROOT_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DEFAULT/lib/common.sh"

BUNDLE=""
PUBKEY=""
DRY_RUN=0
LIVE_DIR="/var/www/empowerx/EmpowerX_AI"
STATE_DIR="/var/lib/empowerx-ai-prod"

usage() {
  printf '%s\n' "usage: install.sh --bundle DIR --pubkey FILE [--dry-run]"
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    --pubkey) PUBKEY="${2:-}"; shift 2 ;;
    --live-dir) LIVE_DIR="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) usage ;;
  esac
done

[[ -n "$BUNDLE" && -n "$PUBKEY" ]] || usage
if [[ "$DRY_RUN" -ne 1 && "$(id -u)" -ne 0 ]]; then
  die 1 "installer must run as root"
fi

required="
bin/deploy.sh
bin/rollback.sh
bin/healthcheck.sh
bin/receive-artifact.sh
bin/package.sh
bin/remote-deploy.sh
lib/common.sh
lib/validate_archive.py
lib/migrate.py
vps/empowerx-ai-prod-shell
vps/empowerx-ai-prod
vps/sudoers-empowerx-ai-prod
vps/ecosystem.config.cjs
"
for rel in $required; do
  [[ -n "$rel" ]] || continue
  [[ -f "$BUNDLE/$rel" ]] || die 4 "bundle is missing $rel"
done

if grep -q "PRIVATE KEY" "$PUBKEY"; then
  die 4 "refusing a private key"
fi
pub="$(awk 'NF && $1 !~ /^#/ {print; exit}' "$PUBKEY")"
case "$pub" in
  ssh-ed25519\ AAAA*|ssh-rsa\ AAAA*|ecdsa-sha2-nistp256\ AAAA*) ;;
  *) die 4 "unsupported public key" ;;
esac
if grep -q "requiretty" "$BUNDLE/vps/sudoers-empowerx-ai-prod"; then
  die 4 "sudoers file contains unsupported requiretty"
fi
visudo -c -f "$BUNDLE/vps/sudoers-empowerx-ai-prod" >/dev/null

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "dry-run: no system changes"
  log "would install production deploy helpers under /usr/local/lib/empowerx-ai-prod"
  log "would leave live runtime, .env, and PM2 processes unchanged"
  exit 0
fi

[[ -d "$LIVE_DIR" ]] || die 4 "live application directory is missing"
[[ -f "$LIVE_DIR/.env" ]] || die 4 "production env file is missing"
[[ -d "$LIVE_DIR/uploads" && -d "$LIVE_DIR/preprocessed" ]] || die 4 "persistent data directories are missing"
[[ -x "$LIVE_DIR/.venv/bin/python" ]] || die 4 "existing production virtualenv is missing"

for tool in pm2 rsync curl tar python3 pg_dump; do
  command -v "$tool" >/dev/null 2>&1 || die 4 "required command missing: $tool"
done

if ! getent group empowerx-ai-deploy >/dev/null 2>&1; then
  groupadd --system empowerx-ai-deploy
fi
if ! id empowerx-ai-deploy >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/empowerx-ai-deploy \
    --shell /bin/bash --gid empowerx-ai-deploy --comment "EmpowerX AI production deploy" \
    empowerx-ai-deploy
fi
chmod 750 /home/empowerx-ai-deploy

if sudo -u empowerx-ai-deploy test -r "$LIVE_DIR/.env"; then
  die 4 "deploy user can read the production env file"
fi

install -d -m 0755 /usr/local/lib/empowerx-ai-prod /etc/empowerx-ai-prod /var/log/empowerx-ai
rm -rf /usr/local/lib/empowerx-ai-prod/deploy
mkdir -p /usr/local/lib/empowerx-ai-prod/deploy
cp -R "$BUNDLE/bin" "$BUNDLE/lib" "$BUNDLE/vps" /usr/local/lib/empowerx-ai-prod/deploy/
chown -R root:root /usr/local/lib/empowerx-ai-prod
find /usr/local/lib/empowerx-ai-prod -type d -exec chmod 755 {} +
find /usr/local/lib/empowerx-ai-prod -type f -exec chmod 644 {} +
find /usr/local/lib/empowerx-ai-prod -type f \( -name '*.sh' -o -name 'empowerx-ai-*' \) -exec chmod 755 {} +

cat > /usr/local/sbin/empowerx-ai-prod <<'EOF'
#!/bin/bash
set -euo pipefail
exec /usr/local/lib/empowerx-ai-prod/deploy/vps/empowerx-ai-prod "$@"
EOF
cat > /usr/local/sbin/empowerx-ai-prod-shell <<'EOF'
#!/bin/bash
set -euo pipefail
exec /usr/local/lib/empowerx-ai-prod/deploy/vps/empowerx-ai-prod-shell "$@"
EOF
chown root:root /usr/local/sbin/empowerx-ai-prod /usr/local/sbin/empowerx-ai-prod-shell
chmod 755 /usr/local/sbin/empowerx-ai-prod /usr/local/sbin/empowerx-ai-prod-shell

tmp_sudo="$(mktemp)"
cp "$BUNDLE/vps/sudoers-empowerx-ai-prod" "$tmp_sudo"
visudo -c -f "$tmp_sudo" >/dev/null
install -m 440 -o root -g root "$tmp_sudo" /etc/sudoers.d/empowerx-ai-prod
rm -f "$tmp_sudo"
visudo -c >/dev/null

LIVE_PY="$LIVE_DIR/.venv/bin/python"
pg_dump_bin="$(command -v pg_dump)"
pm2_bin="$(command -v pm2)"
cat > /etc/empowerx-ai-prod/deploy.env <<EOF
EMPOWERX_AI_LIVE_DIR=$LIVE_DIR
EMPOWERX_AI_STATE_DIR=$STATE_DIR
EMPOWERX_AI_PYTHON=$LIVE_PY
EMPOWERX_AI_HEALTH_URL=http://127.0.0.1:8002/health
EMPOWERX_AI_API_PM2=empowerx-ai-api
EMPOWERX_AI_WORKER_PM2=empowerx-ai-worker
EMPOWERX_AI_MIN_FREE_KB=1048576
EMPOWERX_AI_PG_DUMP=$pg_dump_bin
EMPOWERX_AI_PM2=$pm2_bin
EOF
chown root:root /etc/empowerx-ai-prod/deploy.env
chmod 644 /etc/empowerx-ai-prod/deploy.env

install -d -m 0755 -o root -g root \
  "$STATE_DIR" \
  "$STATE_DIR/backups" \
  "$STATE_DIR/backups/code" \
  "$STATE_DIR/backups/migrations" \
  "$STATE_DIR/venvs" \
  "$STATE_DIR/releases" \
  "$STATE_DIR/state"
install -d -m 0700 -o empowerx-ai-deploy -g empowerx-ai-deploy \
  "$STATE_DIR/incoming" \
  "$STATE_DIR/locks" \
  "$STATE_DIR/logs"

home="$(getent passwd empowerx-ai-deploy | awk -F: '{print $6}')"
install -d -m 700 -o empowerx-ai-deploy -g empowerx-ai-deploy "$home/.ssh"
keyfile="$home/.ssh/authorized_keys"
touch "$keyfile"
chown empowerx-ai-deploy:empowerx-ai-deploy "$keyfile"
chmod 600 "$keyfile"
restricted="restrict,command=\"/usr/local/sbin/empowerx-ai-prod-shell\" $pub"
if ! grep -qxF "$restricted" "$keyfile"; then
  printf '%s\n' "$restricted" >> "$keyfile"
fi

for app in empowerx-ai-api empowerx-ai-worker; do
  pm2 describe "$app" >/dev/null
done

export EMPOWERX_AI_LIVE_DIR="$LIVE_DIR"
export EMPOWERX_AI_STATE_DIR="$STATE_DIR"
mkdir -p "$STATE_DIR/backups/code/baseline"
rsync -a --checksum \
  --exclude '.env' \
  --exclude '.env.local' \
  --exclude 'uploads' \
  --exclude 'preprocessed' \
  --exclude '.venv' \
  --exclude '.git' \
  "$LIVE_DIR"/ "$STATE_DIR/backups/code/baseline"/
if [[ -x "$LIVE_DIR/.venv/bin/python" && ! -d "$STATE_DIR/venvs/baseline" && ! -L "$LIVE_DIR/.venv" ]]; then
  log "preserving existing virtualenv in place until the first deploy"
fi
printf 'baseline\n' > "$STATE_DIR/state/baseline"
log "EmpowerX AI production deploy account installed"
log "live runtime, .env, uploads, database, Redis, and PM2 processes were left running"
