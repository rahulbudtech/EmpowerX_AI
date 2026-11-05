"""Production CI/CD tests. Staging deploy scripts are not exercised here."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
import textwrap
from pathlib import Path

from tests.deploy.support import (
    BASELINE,
    NEW,
    REPO,
    SECRET,
    assert_clean_log,
    assert_persistent,
    gzip_bytes,
    regular,
    tar_gz,
    write_exe,
)

PROD = REPO / "deploy/production"


def prod_bundle(commit: str, version: str, branch: str = "production") -> tuple[bytes, str]:
    files = {
        "app/__init__.py": b"",
        "app/VERSION": (version + "\n").encode(),
        "requirements.txt": b"# test requirements\n",
        "alembic.ini": b"[alembic]\nscript_location = migrations\n",
        "pyproject.toml": b"[project]\nname = 'fixture'\nversion = '0'\n",
        "migrations/versions/keep.py": b"revision = 'keep'\n",
    }
    infos = []
    payloads = {}
    for name, data in files.items():
        info, payload = regular(name, data)
        infos.append(info)
        payloads[info.name] = payload
    inner = tar_gz(infos, payloads)
    inner_sha = hashlib.sha256(inner).hexdigest()
    manifest = (
        f"commit={commit}\n"
        f"archive_sha256={inner_sha}\n"
        f"branch={branch}\n"
        "created_at=2026-11-05T00:00:00Z\n"
    ).encode()
    manifest_info, _ = regular("manifest.txt", manifest)
    inner_info = tarfile.TarInfo("source.tar.gz")
    inner_info.size = len(inner)
    inner_info.mode = 0o644
    inner_info.type = tarfile.REGTYPE
    outer = tar_gz([manifest_info, inner_info], {"manifest.txt": manifest, "source.tar.gz": inner})
    return outer, hashlib.sha256(outer).hexdigest()


def make_prod_layout(tmp: Path) -> dict:
    live = tmp / "live"
    state = tmp / "state"
    (live / "uploads").mkdir(parents=True)
    (live / "preprocessed").mkdir(parents=True)
    (live / "app").mkdir()
    (live / "migrations" / "versions").mkdir(parents=True)
    (live / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://app:{0}@127.0.0.1:5432/empowerx\nMARKER={0}\n".format(SECRET)
    )
    (live / ".env").chmod(0o640)
    (live / "uploads" / "keep.txt").write_text("upload-keep\n")
    (live / "preprocessed" / "keep.txt").write_text("pre-keep\n")
    (live / "app" / "__init__.py").write_text("")
    (live / "app" / "VERSION").write_text("v1\n")
    (live / "requirements.txt").write_text("# test requirements\n")
    (live / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
    (live / "pyproject.toml").write_text("[project]\nname = 'fixture'\nversion = '0'\n")
    (live / "migrations" / "versions" / "keep.py").write_text("revision = 'keep'\n")
    write_exe(live / ".venv" / "bin" / "python", "#!/bin/bash\nexit 0\n")
    bin_dir = state / "bin"
    write_exe(
        bin_dir / "curl",
        textwrap.dedent(
            f"""\
            #!/bin/bash
            output=""
            while [[ $# -gt 0 ]]; do
              case "$1" in
                --max-time) shift 2 ;;
                --output) output="$2"; shift 2 ;;
                --write-out|--silent|--show-error) shift ;;
                *) shift ;;
              esac
            done
            mode="ok"
            [[ -f "{state}/curl-mode" ]] && mode="$(cat "{state}/curl-mode")"
            phase="${{EMPOWERX_AI_HEALTH_PHASE:-deploy}}"
            if [[ "$mode" == "fail-deploy" && "$phase" == "deploy" ]]; then
              exit 7
            fi
            printf '%s' '{{"status":"ok","redis":"ok"}}' > "$output"
            printf '200'
            exit 0
            """
        ),
    )
    write_exe(
        bin_dir / "pm2",
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> "{state}/pm2-calls"
            cmd="${{1:-}}"
            case "$cmd" in
              jlist)
                printf '%s\\n' '[{{"name":"empowerx-ai-api","pid":11}},{{"name":"empowerx-ai-worker","pid":12}},{{"name":"empowerx-api","pid":4242}},{{"name":"empowerx-frontend","pid":3000}},{{"name":"empowerx-facility","pid":3001}}]'
                ;;
              describe|restart)
                exit 0
                ;;
              *)
                echo "unexpected pm2 command" >&2
                exit 1
                ;;
            esac
            """
        ),
    )
    (state / "stub-runtime").mkdir(parents=True)
    (state / "stub-runtime" / "migrations.exit").write_text("0\n")
    (state / "curl-mode").write_text("ok\n")
    env = os.environ.copy()
    env.update(
        {
            "EMPOWERX_AI_PROD_TEST": "1",
            "EMPOWERX_AI_LIVE_DIR": str(live),
            "EMPOWERX_AI_STATE_DIR": str(state),
            "EMPOWERX_AI_PYTHON": "python3",
            "EMPOWERX_AI_CURL": str(bin_dir / "curl"),
            "EMPOWERX_PM2": str(bin_dir / "pm2"),
            "EMPOWERX_AI_HEALTH_TIMEOUT": "2",
            "EMPOWERX_AI_HEALTH_INTERVAL": "0",
            "EMPOWERX_AI_CURL_MAX_TIME": "1",
            "EMPOWERX_AI_WORKER_CHECK_ATTEMPTS": "2",
            "EMPOWERX_AI_WORKER_CHECK_DELAY": "0",
            "EMPOWERX_AI_PM2_CHECK_ATTEMPTS": "2",
            "EMPOWERX_AI_PM2_CHECK_DELAY": "0",
            "EMPOWERX_AI_LOCK_TRIES": "30",
            "EMPOWERX_AI_MIN_FREE_KB": "1",
            "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
        }
    )
    return {"live": live, "state": state, "env": env}


def stage_prod(state: Path, commit: str, version: str, branch: str = "production") -> None:
    payload, digest = prod_bundle(commit, version, branch=branch)
    incoming = state / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / f"{commit}.tar.gz").write_bytes(payload)
    (incoming / f"{commit}.sha256").write_text(digest + "\n")


def run_prod(script: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PROD / script), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_production_workflow_is_isolated() -> None:
    staging = (REPO / ".github/workflows/ai-cicd.yml").read_text()
    production = (REPO / ".github/workflows/ai-production.yml").read_text()
    assert "bud_staging" not in production
    assert "environment: production" in production
    assert "github.ref == 'refs/heads/production'" in production
    assert "deploy/production/bin/remote-deploy.sh" in production
    assert "deploy/bin/remote-deploy.sh" not in production
    assert "environment: development" not in production
    assert "srv2028039" not in production
    assert staging != production
    assert "on:" in staging
    ecosystem = (PROD / "vps/ecosystem.config.cjs").read_text()
    assert "/var/www/empowerx/EmpowerX_AI" in ecosystem
    assert "--concurrency=2" in ecosystem
    assert "8002" in ecosystem
    assert "radiology,celery" in ecosystem
    sudoers = (PROD / "vps/sudoers-empowerx-ai-prod").read_text()
    assert "requiretty" not in sudoers
    assert "/usr/local/sbin/empowerx-ai-prod" in sudoers


def test_production_successful_deploy_preserves_env(tmp_path: Path) -> None:
    layout = make_prod_layout(tmp_path)
    stage_prod(layout["state"], NEW, "v2")
    result = run_prod("bin/deploy.sh", [NEW], layout["env"])
    assert result.returncode == 0, result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v2\n"
    assert (layout["live"] / ".deploy-sha").read_text().strip() == NEW
    assert_persistent(layout["live"])
    calls = (layout["state"] / "pm2-calls").read_text()
    assert "restart empowerx-ai-api" in calls
    assert "restart empowerx-ai-worker" in calls
    assert " all" not in calls
    assert "empowerx-frontend" not in calls
    assert_clean_log(result.stderr)


def test_production_wrong_branch_is_rejected(tmp_path: Path) -> None:
    layout = make_prod_layout(tmp_path)
    stage_prod(layout["state"], NEW, "v2", branch="feature")
    result = run_prod("bin/deploy.sh", [NEW], layout["env"])
    assert result.returncode == 3, result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_persistent(layout["live"])


def test_production_failed_health_rolls_back_code(tmp_path: Path) -> None:
    layout = make_prod_layout(tmp_path)
    (layout["state"] / "curl-mode").write_text("fail-deploy\n")
    stage_prod(layout["state"], NEW, "v2")
    result = run_prod("bin/deploy.sh", [NEW], layout["env"])
    assert result.returncode == 8, result.stderr
    assert "rolled back" in result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_persistent(layout["live"])
    assert_clean_log(result.stderr)


def test_production_pending_migration_is_applied(tmp_path: Path) -> None:
    layout = make_prod_layout(tmp_path)
    (layout["state"] / "stub-runtime" / "migrations.exit").write_text("10\n")
    stage_prod(layout["state"], NEW, "v2")
    result = run_prod("bin/deploy.sh", [NEW], layout["env"])
    assert result.returncode == 0, result.stderr
    assert "apply " in (layout["state"] / "stub-runtime" / "calls").read_text()
    assert (layout["live"] / "app" / "VERSION").read_text() == "v2\n"


def test_production_failed_migration_blocks_activation(tmp_path: Path) -> None:
    layout = make_prod_layout(tmp_path)
    (layout["state"] / "stub-runtime" / "migrations.exit").write_text("10\n")
    (layout["state"] / "stub-runtime" / "migrate.exit").write_text("1\n")
    stage_prod(layout["state"], NEW, "v2")
    result = run_prod("bin/deploy.sh", [NEW], layout["env"])
    assert result.returncode == 6, result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_persistent(layout["live"])


def test_production_package_records_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "migrations" / "versions").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "VERSION").write_text("v9\n")
    (repo / "requirements.txt").write_text("# test\n")
    (repo / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
    (repo / "pyproject.toml").write_text("[project]\nname='t'\nversion='0'\n")
    (repo / "migrations" / "versions" / "keep.py").write_text("revision = 'keep'\n")
    subprocess.run(["git", "init", "-b", "production"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=deploy@example.com", "-c", "user.name=Deploy", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    out = tmp_path / "out.tar.gz"
    env = os.environ.copy()
    env.pop("GITHUB_REF", None)
    env.pop("GITHUB_REF_NAME", None)
    env["EMPOWERX_AI_REPO"] = str(repo)
    result = subprocess.run(
        ["bash", str(PROD / "bin/package.sh"), sha, str(out)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    with tarfile.open(out, "r:gz") as archive:
        manifest = archive.extractfile("manifest.txt")
        assert manifest is not None
        assert "branch=production\n" in manifest.read().decode()


def test_production_install_dry_run(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", "prod-deploy-test"],
        check=True,
        capture_output=True,
    )
    dry = subprocess.run(
        [
            "bash",
            str(PROD / "vps/install.sh"),
            "--dry-run",
            "--bundle",
            str(PROD),
            "--pubkey",
            str(key) + ".pub",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "no system changes" in dry.stderr


def test_production_shell_rejects_metacharacters(tmp_path: Path) -> None:
    layout = make_prod_layout(tmp_path)
    rejected = subprocess.run(
        ["bash", str(PROD / "vps/empowerx-ai-prod-shell")],
        env={**layout["env"], "EMPOWERX_AI_ORIGINAL_COMMAND": "deploy $(uname)"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
