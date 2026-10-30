#!/bin/bash
# One-time, repeatable installer for the EmpowerX AI deploy account.
# It does not restart PM2, Nginx, PostgreSQL, or Redis.
set -euo pipefail

ROOT_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT_DEFAULT/lib/common.sh"

BUNDLE=""
PUBKEY=""
DRY_RUN=0
LIVE_DIR="/var/www/empowerx/EmpowerX-AI"
STATE_DIR="/var/lib/empowerx-ai"
BASELINE_SHA="8238ab968840be4530458e7cabfc508391906dc4"
MANAGED_PY="/opt/empowerx-ai/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12"

usage() {
  printf '%s\n' "usage: install.sh --bundle DIR --pubkey FILE [--baseline-sha SHA] [--dry-run]"
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle) BUNDLE="${2:-}"; shift 2 ;;
    --pubkey) PUBKEY="${2:-}"; shift 2 ;;
    --baseline-sha) BASELINE_SHA="${2:-}"; shift 2 ;;
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
bin/backup-baseline.sh
bin/package-artifact.sh
bin/remote-deploy.sh
lib/common.sh
lib/release.sh
lib/atomic_symlink.py
lib/validate_archive.py
lib/scan_migrations.py
lib/migration_status.py
vps/empowerx-ai-deploy-shell
vps/empowerx-ai-pm2
vps/empowerx-ai-activate
vps/empowerx-ai-runtime
vps/empowerx-ai-migrate
vps/sudoers-empowerx-ai-deploy
vps/ecosystem.config.cjs
vps/deploy.env.example
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
if grep -q "requiretty" "$BUNDLE/vps/sudoers-empowerx-ai-deploy"; then
  die 4 "sudoers file contains unsupported requiretty"
fi
visudo -c -f "$BUNDLE/vps/sudoers-empowerx-ai-deploy" >/dev/null

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "dry-run: no system changes"
  log "would create user empowerx-ai-deploy and install restricted SSH key"
  log "would install helpers under /usr/local/lib/empowerx-ai and /usr/local/sbin"
  log "would record baseline $BASELINE_SHA from $LIVE_DIR"
  exit 0
fi

assert_sha "$BASELINE_SHA" || die 2 "invalid baseline sha"
[[ -d "$LIVE_DIR" ]] || die 4 "live application directory is missing"
[[ -f "$LIVE_DIR/.env" ]] || die 4 "production env file is missing"
[[ -d "$LIVE_DIR/uploads" && -d "$LIVE_DIR/preprocessed" ]] || die 4 "persistent data directories are missing"
if [[ -d "$LIVE_DIR/.git" ]]; then
  head_sha="$(git -C "$LIVE_DIR" rev-parse HEAD)"
  case "$head_sha" in
    "$BASELINE_SHA") ;;
    *) die 4 "live checkout $head_sha does not match baseline $BASELINE_SHA" ;;
  esac
fi

[[ -x "$MANAGED_PY" ]] || die 4 "managed Python 3.12 is missing"
case "$MANAGED_PY" in
  /opt/empowerx-ai/*) ;;
  *) die 4 "refusing a Python outside /opt/empowerx-ai" ;;
esac
"$MANAGED_PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' \
  || die 4 "managed interpreter is not Python 3.12"

if [[ -x /root/.local/bin/uv ]]; then
  install -m 0755 /root/.local/bin/uv /usr/local/bin/uv
fi
[[ -x /usr/local/bin/uv ]] || die 4 "uv is not available at /usr/local/bin/uv"
/usr/local/bin/uv --version >/dev/null

for tool in pm2 rsync curl tar runuser ssh-keygen; do
  command -v "$tool" >/dev/null 2>&1 || die 4 "required command missing: $tool"
done
command -v setfacl >/dev/null 2>&1 || die 4 "required command missing: setfacl (acl package is not installed)"

pg_dump_bin=""
for candidate in /usr/lib/postgresql/18/bin/pg_dump /usr/bin/pg_dump; do
  if [[ -x "$candidate" ]]; then
    pg_dump_bin="$candidate"
    break
  fi
done
if [[ -z "$pg_dump_bin" ]]; then
  pg_dump_bin="$(command -v pg_dump || true)"
fi
[[ -n "$pg_dump_bin" ]] || die 4 "pg_dump is missing"

id empowerx-ai >/dev/null 2>&1 || die 4 "runtime user empowerx-ai is missing"
if ! getent group empowerx-ai-deploy >/dev/null 2>&1; then
  groupadd --system empowerx-ai-deploy
fi
if ! id empowerx-ai-deploy >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/empowerx-ai-deploy \
    --shell /bin/bash --gid empowerx-ai-deploy --comment "EmpowerX AI deploy" \
    empowerx-ai-deploy
fi
chmod 750 /home/empowerx-ai-deploy

if sudo -u empowerx-ai-deploy test -w "$LIVE_DIR"; then
  die 4 "deploy user can write the live directory"
fi
if sudo -u empowerx-ai-deploy test -r "$LIVE_DIR/.env"; then
  die 4 "deploy user can read the production env file"
fi

install -d -m 0755 /usr/local/lib/empowerx-ai /etc/empowerx-ai /var/log/empowerx-ai
rm -rf /usr/local/lib/empowerx-ai/deploy
mkdir -p /usr/local/lib/empowerx-ai/deploy
cp -R "$BUNDLE/bin" "$BUNDLE/lib" "$BUNDLE/vps" /usr/local/lib/empowerx-ai/deploy/
chown -R root:root /usr/local/lib/empowerx-ai/deploy
find /usr/local/lib/empowerx-ai/deploy -type d -exec chmod 755 {} +
find /usr/local/lib/empowerx-ai/deploy -type f -exec chmod 644 {} +
find /usr/local/lib/empowerx-ai/deploy -type f \( -name '*.sh' -o -name 'empowerx-ai-*' \) -exec chmod 755 {} +

write_wrapper() {
  local name="$1"
  cat > "/usr/local/sbin/$name" <<EOF
#!/bin/bash
set -euo pipefail
exec /usr/local/lib/empowerx-ai/deploy/vps/$name "\$@"
EOF
  chown root:root "/usr/local/sbin/$name"
  chmod 755 "/usr/local/sbin/$name"
}
write_wrapper empowerx-ai-deploy-shell
write_wrapper empowerx-ai-pm2
write_wrapper empowerx-ai-activate
write_wrapper empowerx-ai-runtime
write_wrapper empowerx-ai-migrate

tmp_sudo="$(mktemp)"
cp "$BUNDLE/vps/sudoers-empowerx-ai-deploy" "$tmp_sudo"
visudo -c -f "$tmp_sudo" >/dev/null
install -m 440 -o root -g root "$tmp_sudo" /etc/sudoers.d/empowerx-ai-deploy
rm -f "$tmp_sudo"
visudo -c >/dev/null

install -m 644 -o root -g root "$BUNDLE/vps/ecosystem.config.cjs" /etc/empowerx-ai/ecosystem.config.cjs
cat > /etc/empowerx-ai/deploy.env <<EOF
EMPOWERX_AI_LIVE_DIR=$LIVE_DIR
EMPOWERX_AI_STATE_DIR=$STATE_DIR
EMPOWERX_AI_PYTHON=$MANAGED_PY
EMPOWERX_AI_UV=/usr/local/bin/uv
EMPOWERX_AI_HEALTH_URL=http://127.0.0.1:8002/health
EMPOWERX_AI_PUBLIC_HEALTH_URL=https://ai.devempowerx.io/health
EMPOWERX_AI_REQUIRE_PUBLIC_HEALTH=yes
EMPOWERX_AI_API_PM2=empowerx-ai-api
EMPOWERX_AI_WORKER_PM2=empowerx-ai-worker
EMPOWERX_AI_BASELINE_SHA=$BASELINE_SHA
EMPOWERX_AI_MIN_FREE_KB=1048576
EMPOWERX_AI_PG_DUMP=$pg_dump_bin
EMPOWERX_AI_ECOSYSTEM=/etc/empowerx-ai/ecosystem.config.cjs
EOF
chown root:root /etc/empowerx-ai/deploy.env
chmod 644 /etc/empowerx-ai/deploy.env

install -d -m 0755 -o root -g root \
  "$STATE_DIR" \
  "$STATE_DIR/backups" \
  "$STATE_DIR/venvs" \
  "$STATE_DIR/catalog" \
  "$STATE_DIR/state"
install -d -m 2750 -o empowerx-ai-deploy -g empowerx-ai "$STATE_DIR/releases"
install -d -m 0700 -o empowerx-ai-deploy -g empowerx-ai-deploy \
  "$STATE_DIR/incoming" \
  "$STATE_DIR/locks" \
  "$STATE_DIR/logs"
install -d -m 0750 -o empowerx-ai -g empowerx-ai /var/log/empowerx-ai

home="$(getent passwd empowerx-ai-deploy | awk -F: '{print $6}')"
install -d -m 700 -o empowerx-ai-deploy -g empowerx-ai-deploy "$home/.ssh"
keyfile="$home/.ssh/authorized_keys"
touch "$keyfile"
chown empowerx-ai-deploy:empowerx-ai-deploy "$keyfile"
chmod 600 "$keyfile"
restricted="restrict,command=\"/usr/local/sbin/empowerx-ai-deploy-shell\" $pub"
if grep -qxF "$restricted" "$keyfile"; then
  log "deploy key already installed"
else
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    case "$line" in
      \#*) continue ;;
      restrict,command=\"/usr/local/sbin/empowerx-ai-deploy-shell\"\ *) ;;
      *) die 4 "authorized_keys contains an unrestricted key" ;;
    esac
  done < "$keyfile"
  printf '%s\n' "$restricted" >> "$keyfile"
fi
if command -v ssh-keygen >/dev/null 2>&1; then
  ssh-keygen -lf "$PUBKEY" || true
fi

# Confirm the protected apps are present without restarting anything.
for app in empowerx-api empowerx-frontend empowerx-facility empowerx-ai-api empowerx-ai-worker; do
  pm2 describe "$app" >/dev/null
done
if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active postgresql >/dev/null 2>&1 || log "postgresql unit was not reported active"
  systemctl is-active redis >/dev/null 2>&1 || systemctl is-active redis-server >/dev/null 2>&1 || log "redis unit was not reported active"
fi

export EMPOWERX_AI_LIVE_DIR="$LIVE_DIR"
export EMPOWERX_AI_STATE_DIR="$STATE_DIR"
export EMPOWERX_AI_PYTHON="$MANAGED_PY"
export EMPOWERX_AI_UV="/usr/local/bin/uv"
export EMPOWERX_AI_BASELINE_SHA="$BASELINE_SHA"
export EMPOWERX_AI_MIN_FREE_KB=1
/usr/local/lib/empowerx-ai/deploy/bin/backup-baseline.sh

printf 'production\n' > /etc/empowerx-ai/production
chmod 644 /etc/empowerx-ai/production
log "EmpowerX AI deploy account installed"
