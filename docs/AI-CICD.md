# EmpowerX AI CI/CD

This repository deploys only the EmpowerX AI API and Celery worker. The Hostinger VPS already runs PostgreSQL, Redis, Nginx, and three other PM2 applications. Those stay in place.

## Architecture

```text
pull request -> bud_development
        |
        v
GitHub Actions validate job
  Python 3.12, dependency install, compile, import, deployment tests
        |
        | merge
        v
push to bud_development
        |
        v
GitHub Actions deploy job
  environment: development
  package git archive + checksum
  SSH as empowerx-ai-deploy (forced command, no TTY, no forwarding)
        |
        v
/var/lib/empowerx-ai/releases/<sha>
  extract, reject unsafe paths, build a new virtualenv with managed Python 3.12
  scan Alembic files
  if the database is already at head: continue
  if migrations are pending: stop unless both approval gates are set
        |
        v
/var/www/empowerx/EmpowerX-AI
  code paths become symlinks to the new release
  .env, uploads, and preprocessed stay as real directories
  .venv becomes a symlink to the release virtualenv
        |
        v
root helper reloads only:
  empowerx-ai-api
  empowerx-ai-worker
        |
        v
checks: http://127.0.0.1:8002/health
        PostgreSQL SELECT 1
        celery inspect ping
        https://ai.devempowerx.io/health
        |
        +-- pass: record the release
        |
        +-- fail: point both processes back at the previous release
                  do not restore or downgrade the shared database
```

The API listens on `127.0.0.1:8002`. Nginx already terminates TLS for `ai.devempowerx.io` and proxies that host to the API. Uvicorn is started without an `/ai` ASGI prefix. The old `--root-path /ai` setting made generated URLs advertise a prefix this dedicated host does not use. Swagger remains at `https://ai.devempowerx.io/docs`. The radiology route remains `POST /radiology/dicom/analyze`.

The managed interpreter is `/opt/empowerx-ai/python/cpython-3.12-linux-x86_64-gnu/bin/python3.12`. The deploy scripts refuse `/usr/bin/python*`. `uv` is installed at `/usr/local/bin/uv` so the deploy user can run it without reading `/root`.

Runtime state:

| Path | Purpose |
| --- | --- |
| `/var/www/empowerx/EmpowerX-AI` | Live tree. `.env`, `uploads`, and `preprocessed` stay here. |
| `/var/lib/empowerx-ai/releases/<sha>` | One immutable code tree and its virtualenv. |
| `/var/lib/empowerx-ai/venvs/baseline` | The virtualenv that was already on the server before the first switch. |
| `/var/lib/empowerx-ai/incoming` | Uploaded bundle, readable only by the deploy user. |
| `/var/lib/empowerx-ai/logs` | Deployment logs with secrets redacted. |
| `/var/log/empowerx-ai` | API and worker process logs. |
| `/etc/empowerx-ai/deploy.env` | Non-secret paths and tool locations. |
| `/etc/empowerx-ai/production` | Marker that disables test hooks. |

The deploy user is `empowerx-ai-deploy`. It is not the runtime user `empowerx-ai`, and it is not a deploy user from another repository. It can sudo only four helper programs:

- `/usr/local/sbin/empowerx-ai-pm2`
- `/usr/local/sbin/empowerx-ai-activate`
- `/usr/local/sbin/empowerx-ai-migrate`
- `/usr/local/sbin/empowerx-ai-runtime` as `empowerx-ai`

`empowerx-ai-pm2 reload-ai` deletes and starts only `empowerx-ai-api` and `empowerx-ai-worker`, then compares the PIDs of `empowerx-api`, `empowerx-frontend`, and `empowerx-facility`. If any of those PIDs change, the helper fails. It has no command that reloads every PM2 process.

## Branch and pull request workflow

`bud_development` is the development and deployment branch. It was created from `development` at `8238ab968840be4530458e7cabfc508391906dc4`, which is also the current production checkout. `main`, `development`, and `production` are left in place.

1. Create a feature branch from `bud_development`.
2. Open a pull request into `bud_development`.
3. GitHub Actions runs the validate job. That job has `contents: read` and no deployment environment, so it cannot read the VPS secrets.
4. After review, merge the pull request. The merge push runs validate again and then the deploy job.
5. Do not merge the CI/CD pull request until the VPS account and GitHub environment below are in place. A merge is a production deploy.

Make `bud_development` the GitHub default branch so new pull requests target it:

1. Open `https://github.com/rahulbudtech/EmpowerX_AI/settings`.
2. Under **Default branch**, switch the branch to `bud_development`.
3. Confirm. Do not delete `main`, `development`, or `production`.

If `gh` is authenticated with permission to administer the repository, the same change is:

```bash
gh repo edit rahulbudtech/EmpowerX_AI --default-branch bud_development
```

## GitHub environment

Create an environment named `development` on `rahulbudtech/EmpowerX_AI`.

Settings → Environments → New environment → `development`.

Do not add required reviewers if a merge should deploy immediately. Required reviewers pause the deploy job until someone approves it.

### Secrets

| Name | Value |
| --- | --- |
| `VPS_SSH_PRIVATE_KEY` | Contents of the deploy private key, including the header and footer lines. |
| `VPS_KNOWN_HOSTS` | Output of `ssh-keyscan` for the VPS, after the fingerprint is checked. |

### Variables

| Name | Value |
| --- | --- |
| `VPS_HOST` | `srv2028039.hstgr.cloud` |
| `VPS_USER` | `empowerx-ai-deploy` |
| `VPS_PORT` | `22` |
| `EMPOWERX_AI_ALLOW_MIGRATIONS` | Leave unset. Set to `yes` only for a reviewed schema change, then clear it. |

The workflow uses `vars.*` for the host settings and `secrets.*` for the key and known hosts. It does not use a personal access token.

## One-time Mac SSH key

Use a new key for this account. Do not reuse a personal key or a key from another EmpowerX repository.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/empowerx_ai_deploy -C "empowerx-ai-deploy" -N ""
ssh-keyscan -p 22 srv2028039.hstgr.cloud | tee /tmp/empowerx-ai-known_hosts
ssh-keygen -lf /tmp/empowerx-ai-known_hosts
```

Compare that fingerprint with the host key shown by the Hostinger console before storing `/tmp/empowerx-ai-known_hosts` in `VPS_KNOWN_HOSTS`. Store the contents of `~/.ssh/empowerx_ai_deploy` in `VPS_SSH_PRIVATE_KEY`.

Copy the public key to the VPS as root:

```bash
scp -P 22 ~/.ssh/empowerx_ai_deploy.pub root@srv2028039.hstgr.cloud:/root/empowerx-ai-deploy.pub
```

## One-time Hostinger installation

Run this from a checkout of the CI/CD branch before that branch is merged. `bud_development` does not contain these files until the merge.

```bash
git fetch origin
git checkout feature/ai-cicd
tar -C deploy -czf /tmp/empowerx-ai-cicd-bundle.tar.gz bin lib vps
scp -P 22 /tmp/empowerx-ai-cicd-bundle.tar.gz root@srv2028039.hstgr.cloud:/root/empowerx-ai-cicd-bundle.tar.gz
```

On the VPS, as root:

```bash
mkdir -p /root/empowerx-ai-cicd-bundle
tar -xzf /root/empowerx-ai-cicd-bundle.tar.gz -C /root/empowerx-ai-cicd-bundle
bash /root/empowerx-ai-cicd-bundle/vps/install.sh \
  --bundle /root/empowerx-ai-cicd-bundle \
  --pubkey /root/empowerx-ai-deploy.pub \
  --baseline-sha 8238ab968840be4530458e7cabfc508391906dc4
```

The installer is safe to re-run after a reviewed script change. It will:

- create `empowerx-ai-deploy` if needed
- install the public key with `restrict` and a forced command
- install the helpers and sudoers file, checking the file with `visudo`
- copy `/root/.local/bin/uv` to `/usr/local/bin/uv` when that root-only binary is present
- verify managed Python 3.12, `pm2`, `rsync`, `curl`, `tar`, `runuser`, and `pg_dump`
- confirm the deploy user cannot write the live tree and cannot read `.env`
- copy the current code to `/var/lib/empowerx-ai/backups/baseline-<sha>` and `releases/<sha>`, excluding `.env`, `uploads`, `preprocessed`, and `.venv`
- refuse to continue if the live git checkout is not the baseline SHA

It does not reload PM2, Nginx, PostgreSQL, or Redis.

Before the installer will succeed, the live tree must already match these permissions:

- `/var/www/empowerx/EmpowerX-AI/.env` is owned by `root:empowerx-ai` and mode `640`
- `uploads` and `preprocessed` exist and are writable by `empowerx-ai`
- `empowerx-ai-deploy` is not a member of the `empowerx-ai` group
- the live directory is not writable by `empowerx-ai-deploy`

If the installer stops because the live checkout SHA differs, pass the full `git -C /var/www/empowerx/EmpowerX-AI rev-parse HEAD` value as `--baseline-sha` only after confirming that checkout is the intended rollback point.

### Files the installer expects in the bundle

```text
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
```

Missing files abort the installer before it creates the user.

## Check SSH before the first deploy

From the Mac:

```bash
ssh -i ~/.ssh/empowerx_ai_deploy \
  -p 22 \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/tmp/empowerx-ai-known_hosts \
  empowerx-ai-deploy@srv2028039.hstgr.cloud \
  status
```

`status` prints the recorded release and runs the health checks. It does not restart processes. An interactive SSH session is rejected. A shell metacharacter in the remote command is rejected.

## First automatic deployment

After the installer, the GitHub environment, and the SSH check:

1. Merge the reviewed CI/CD pull request into `bud_development`.
2. The push runs validation, then the deploy job.
3. The job packages `github.sha`, uploads it, and runs `deploy <sha>` on the VPS.
4. The first activation moves the existing virtualenv to `/var/lib/empowerx-ai/venvs/baseline` and links the live `.venv` at that path before switching it to the new release. The API keeps a working interpreter path during the move.
5. PM2 replaces only the two AI process definitions, which drops `--root-path /ai`.
6. Health, database, and worker checks must pass. Failure restores the baseline release and reloads the same two processes.

Watch the run at `https://github.com/rahulbudtech/EmpowerX_AI/actions`.

## Normal deployments

1. Open a pull request into `bud_development`.
2. Wait for the validate job.
3. Merge.
4. The deploy job packages that merge commit and activates it.
5. `.env`, `uploads`, and `preprocessed` are not in the bundle and are not replaced.
6. The previous healthy release stays recorded for rollback.
7. Releases other than the current, previous, and baseline copies are pruned after five newer copies remain.

Deployments do not run `git reset` or `git clean` in `/var/www/empowerx/EmpowerX-AI`.

## Logs, troubleshooting, and rollback

Deployment logs:

```bash
ls -lt /var/lib/empowerx-ai/logs
```

Process logs:

```bash
tail -n 100 /var/log/empowerx-ai/api.err.log
tail -n 100 /var/log/empowerx-ai/worker.err.log
```

PM2 status for the AI processes only:

```bash
sudo /usr/local/sbin/empowerx-ai-pm2 status-ai
```

Manual rollback to the previous healthy release, without a database change:

```bash
ssh -i ~/.ssh/empowerx_ai_deploy \
  -p 22 \
  -o BatchMode=yes \
  -o IdentitiesOnly=yes \
  -o UserKnownHostsFile=/tmp/empowerx-ai-known_hosts \
  empowerx-ai-deploy@srv2028039.hstgr.cloud \
  rollback
```

On the server the same action is `sudo /usr/local/sbin/empowerx-ai-activate switch <previous-sha>` followed by `sudo /usr/local/sbin/empowerx-ai-pm2 reload-ai`. Prefer the `rollback` SSH command so the health check runs too.

If a deploy is interrupted after it has switched files but before it has recorded success, `/var/lib/empowerx-ai/state/current` still contains the last healthy SHA. Restore it with:

```bash
sudo /usr/local/sbin/empowerx-ai-activate switch "$(cat /var/lib/empowerx-ai/state/current)"
sudo /usr/local/sbin/empowerx-ai-pm2 reload-ai
```

A failed health check during an automatic deploy already does this and exits non-zero so the GitHub job is red.

Common failures:

| Exit | Meaning |
| --- | --- |
| 2 | The commit SHA or remote command is not valid. |
| 3 | The bundle is missing, truncated, or rejected. |
| 4 | Configuration, permissions, or disk space blocked the deploy. |
| 5 | Python 3.12 virtualenv or dependency installation failed. The live tree was not switched. |
| 6 | Migration status could not be read, or a schema change was not approved. |
| 8 | The new release was activated, health failed, and the previous release is back in service. |
| 9 | Health failed and the automatic rollback also failed. Restore `state/current` manually. |

## Production migration safety

EmpowerX AI and the Node.js backend share the PostgreSQL database `empowerx`. The five AI revisions through `20260424_0005` are already applied. A normal code deploy runs `check` only. It does not run `upgrade`.

Pending revisions stop the deploy before the live tree changes. Two independent approvals are required:

1. On the VPS, as root, write the exact incoming Alembic head:

```bash
printf '%s\n' "20260424_0006" > /etc/empowerx-ai/allow-migrations
chmod 644 /etc/empowerx-ai/allow-migrations
```

Replace the revision with the head printed by the failed deploy log.

2. In the GitHub `development` environment, set `EMPOWERX_AI_ALLOW_MIGRATIONS` to `yes`.

Re-run the failed deploy job. The migrate helper:

- refuses a head that does not match the approval file
- refuses an `alembic_version` row that belongs to another application
- runs `pg_dump` of only `radiology_findings` and `alembic_version`
- stores that dump at `/var/lib/empowerx-ai/backups/migrations/`, mode `600`, owner `root`
- runs `alembic upgrade head`
- renames the approval file so it cannot be reused

Clear `EMPOWERX_AI_ALLOW_MIGRATIONS` after the job. Code rollback does not run `alembic downgrade` and does not run `pg_restore`. Restoring a dump is a separate, manual decision because it would rewrite rows in the shared database.

The migration scanner rejects revisions that drop, truncate, delete, or alter a table other than `radiology_findings` or `alembic_version`. That scan runs in pull-request validation and again on the server before a build is activated.

## Verify the domain and the worker

On the VPS:

```bash
curl --fail --silent http://127.0.0.1:8002/health
curl --fail --silent https://ai.devempowerx.io/health
curl --fail --silent https://ai.devempowerx.io/docs >/dev/null
sudo -n -u empowerx-ai /usr/local/sbin/empowerx-ai-runtime check-database
sudo -n -u empowerx-ai /usr/local/sbin/empowerx-ai-runtime check-worker
```

A healthy API body is `{"status":"ok","redis":"ok"}`. The worker check prints nothing on success and returns exit code 0 when Celery answers `pong`.

From a workstation:

```bash
curl --fail --silent https://ai.devempowerx.io/health
```

Confirm the other applications were not reloaded:

```bash
sudo pm2 jlist | python3 -c 'import json,sys; data=json.load(sys.stdin)
for name in ("empowerx-api","empowerx-frontend","empowerx-facility","empowerx-ai-api","empowerx-ai-worker"):
    proc=next(item for item in data if item.get("name")==name)
    print(name, proc.get("pid"), proc.get("pm2_env",{}).get("status"))'
```

The first three PIDs should match the values recorded before the deploy.

## Local validation

```bash
bash deploy/bin/run-local-checks.sh
```

That command checks shell syntax, migration safety, the sudoers file, and `tests/deploy`. It exits non-zero when pytest collects nothing or reports a skip. The repository does not yet contain application unit tests under `tests/unit`; the CI job still imports `app.main` and `app.workers.celery_app` after installing `requirements.txt` on Python 3.12.

GitHub Actions is the place that installs the full scientific dependency set and runs that import. The local script reports the import as not run when those packages are absent.
