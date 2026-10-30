"""Deployment safety tests. These execute the bash tooling against temp directories."""

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
    bundle_bytes,
    gzip_bytes,
    make_layout,
    regular,
    run,
    stage_bundle,
    tar_gz,
    write_exe,
)


def test_invalid_commit_sha(tmp_path: Path) -> None:
    result = run("deploy/bin/deploy.sh", ["not-a-sha"], os.environ.copy())
    assert result.returncode == 2
    assert not (tmp_path / "live").exists()


def test_missing_and_malformed_artifacts(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    missing = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert missing.returncode == 3
    assert_persistent(layout["live"])
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert not (layout["live"] / "app").is_symlink()

    payload = b"this is not an archive"
    incoming = layout["state"] / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / f"{NEW}.tar.gz").write_bytes(payload)
    (incoming / f"{NEW}.sha256").write_text(hashlib.sha256(payload).hexdigest() + "\n")
    malformed = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert malformed.returncode == 3
    assert_persistent(layout["live"])
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_clean_log(malformed.stderr)


def test_path_traversal_symlink_and_secret_paths(tmp_path: Path) -> None:
    sentinel = tmp_path / "escaped"
    sentinel.write_text("safe\n")
    traversal = tarfile.TarInfo("../escaped")
    traversal.size = 4
    traversal.type = tarfile.REGTYPE
    link = tarfile.TarInfo("app/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    absolute = tarfile.TarInfo("/tmp/empowerx-absolute")
    absolute.size = 4
    absolute.type = tarfile.REGTYPE
    env_info, env_data = regular(".env", b"TOKEN=nope\n")
    upload_info, upload_data = regular("uploads/evil.txt", b"nope\n")

    for info, payloads in (
        (traversal, {"../escaped": b"evil"}),
        (link, {}),
        (absolute, {"/tmp/empowerx-absolute": b"evil"}),
        (env_info, {".env": env_data}),
        (upload_info, {"uploads/evil.txt": upload_data}),
    ):
        blob = tar_gz([info], payloads)
        archive = tmp_path / "bad.tar.gz"
        archive.write_bytes(blob)
        dest = tmp_path / "out"
        result = subprocess.run(
            ["python3", str(REPO / "deploy/lib/validate_archive.py"), "extract", "--archive", str(archive), "--dest", str(dest)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 3, result.stderr
        assert not dest.exists()
    assert sentinel.read_text() == "safe\n"

    inner_members = []
    inner_payloads = {}
    for info, data in [
        regular("app/VERSION", b"v2\n"),
        regular("../escaped", b"evil"),
    ]:
        inner_members.append(info)
        inner_payloads[info.name] = data
    malicious_inner = tar_gz(inner_members, inner_payloads)
    layout = make_layout(tmp_path / "layout")
    stage_bundle(layout["state"], NEW, "v2", inner=malicious_inner)
    deployed = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert deployed.returncode == 3
    assert sentinel.read_text() == "safe\n"
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_persistent(layout["live"])


def test_missing_environment_configuration(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("EMPOWERX_AI_LIVE_DIR", None)
    env.pop("EMPOWERX_AI_STATE_DIR", None)
    result = run("deploy/bin/deploy.sh", [NEW], env)
    assert result.returncode == 4

    layout = make_layout(tmp_path)
    (layout["live"] / ".env").unlink()
    stage_bundle(layout["state"], NEW, "v2")
    removed = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert removed.returncode == 4
    assert (layout["live"] / "uploads" / "keep.txt").read_text() == "upload-keep\n"
    assert not (layout["live"] / "app").is_symlink()


def test_permission_failure_leaves_live_tree(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stage_bundle(layout["state"], NEW, "v2")
    archive = layout["state"] / "incoming" / f"{NEW}.tar.gz"
    archive.chmod(0)
    try:
        result = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    finally:
        archive.chmod(0o600)
    assert result.returncode == 4
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert not (layout["live"] / "app").is_symlink()
    assert_persistent(layout["live"])


def test_failed_dependency_installation(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    (layout["state"] / "fail-deps").write_text("1\n")
    stage_bundle(layout["state"], NEW, "v2")
    result = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert result.returncode == 5
    assert "dependency installation failed" in result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert not (layout["live"] / "app").is_symlink()
    assert (layout["live"] / ".venv").is_dir()
    assert not (layout["live"] / ".venv").is_symlink()
    assert_persistent(layout["live"])
    assert_clean_log(result.stderr)


def _deploy_and_expect_rollback(tmp_path: Path, *, curl_mode: str | None = None, worker_mode: str | None = None) -> tuple[dict, subprocess.CompletedProcess[str]]:
    layout = make_layout(tmp_path)
    if curl_mode:
        (layout["state"] / "curl-mode").write_text(curl_mode + "\n")
    if worker_mode:
        (layout["state"] / "stub-runtime" / "worker-mode").write_text(worker_mode + "\n")
    stage_bundle(layout["state"], NEW, "v2")
    result = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    return layout, result


def test_fastapi_startup_failure_rolls_back(tmp_path: Path) -> None:
    layout, result = _deploy_and_expect_rollback(tmp_path, curl_mode="fail-deploy")
    assert result.returncode == 8, result.stderr
    assert "FastAPI health check failed" in result.stderr
    assert "rolled back" in result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert str(BASELINE) in os.readlink(layout["live"] / "app")
    assert os.readlink(layout["live"] / ".venv") == str(layout["state"] / "venvs" / "baseline")
    assert_persistent(layout["live"])
    calls = (layout["state"] / "stub-runtime" / "calls").read_text()
    assert "apply " not in calls
    assert "downgrade" not in calls
    assert_clean_log(result.stderr)


def test_worker_startup_failure_rolls_back(tmp_path: Path) -> None:
    layout, result = _deploy_and_expect_rollback(tmp_path, worker_mode="fail-deploy")
    assert result.returncode == 8, result.stderr
    assert "Celery worker check failed" in result.stderr
    assert "rolled back" in result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_persistent(layout["live"])
    assert_clean_log(result.stderr)
    worker_calls = [
        line
        for line in (layout["state"] / "stub-runtime" / "calls").read_text().splitlines()
        if line.strip() == "check-worker"
    ]
    # Five failed deploy attempts, then one successful rollback check.
    assert len(worker_calls) == 6


def test_celery_worker_retry_succeeds(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stub = layout["state"] / "stub-runtime"
    (stub / "worker-mode").write_text("fail-then-ok\n")
    (stub / "worker-fail-until").write_text("2\n")
    layout["env"]["EMPOWERX_AI_HEALTH_PHASE"] = "deploy"
    result = run("deploy/bin/healthcheck.sh", [], layout["env"])
    assert result.returncode == 0, result.stderr
    calls = (stub / "calls").read_text().splitlines()
    assert calls.count("check-worker") == 3
    assert "Celery worker responded" in result.stderr
    assert "retrying" in result.stderr
    assert_clean_log(result.stderr)


def test_celery_worker_retries_exhausted(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stub = layout["state"] / "stub-runtime"
    (stub / "worker-mode").write_text("fail-deploy\n")
    layout["env"]["EMPOWERX_AI_HEALTH_PHASE"] = "deploy"
    result = run("deploy/bin/healthcheck.sh", [], layout["env"])
    assert result.returncode == 22, result.stderr
    calls = (stub / "calls").read_text().splitlines()
    assert calls.count("check-worker") == 5
    assert "Celery worker check failed" in result.stderr
    assert "Celery worker responded" not in result.stderr
    assert_clean_log(result.stderr)


def test_runtime_acl_failure_leaves_env_untouched(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    release = layout["state"] / "releases" / NEW
    release.mkdir(parents=True, exist_ok=True)
    shutil.copytree(layout["live"] / "app", release / "app")
    shutil.copytree(layout["live"] / "migrations", release / "migrations")
    for name in ("requirements.txt", "alembic.ini", "pyproject.toml"):
        shutil.copy2(layout["live"] / name, release / name)
    log_path = layout["state"] / "setfacl.log"
    write_exe(
        layout["state"] / "bin" / "setfacl",
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> "{log_path}"
            exit 1
            """
        ),
    )
    env_path = layout["live"] / ".env"
    env_mode = stat.S_IMODE(env_path.stat().st_mode)
    env_text = env_path.read_text()
    script = textwrap.dedent(
        f"""\
        source "{REPO}/deploy/lib/common.sh"
        source "{REPO}/deploy/lib/release.sh"
        production_mode() {{ return 0; }}
        build_release "{release}"
        """
    )
    result = subprocess.run(
        ["bash", "-c", script],
        env=layout["env"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 5, result.stderr
    assert "runtime ACL setup failed" in result.stderr
    assert not (release / ".release-complete").exists()
    assert env_path.read_text() == env_text
    assert not env_path.is_symlink()
    assert stat.S_IMODE(env_path.stat().st_mode) == env_mode
    assert stat.S_IMODE(env_path.stat().st_mode) & stat.S_IWOTH == 0
    assert stat.S_IMODE(env_path.stat().st_mode) & stat.S_IROTH == 0
    logged = log_path.read_text()
    assert "-R -m u:empowerx-ai:rX" in logged
    assert str(release / ".venv") in logged
    assert ".env" not in logged
    assert str(layout["live"] / ".env") not in logged
    assert_persistent(layout["live"])
    assert_clean_log(result.stderr)


def test_installer_requires_setfacl_without_installing_packages() -> None:
    text = (REPO / "deploy/vps/install.sh").read_text()
    assert "command -v setfacl" in text
    assert "acl package is not installed" in text
    lowered = text.lower()
    assert "apt-get" not in lowered
    assert "apt install" not in lowered
    assert "dnf install" not in lowered
    assert "yum install" not in lowered
    assert "apk add" not in lowered


def test_health_check_timeout_rolls_back(tmp_path: Path) -> None:
    layout, result = _deploy_and_expect_rollback(tmp_path, curl_mode="timeout")
    assert result.returncode == 8, result.stderr
    assert "timed out" in result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_persistent(layout["live"])


def test_successful_deploy_preserves_data_and_other_pm2_apps(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stage_bundle(layout["state"], NEW, "v2")
    result = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert result.returncode == 0, result.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v2\n"
    assert str(NEW) in os.readlink(layout["live"] / "app")
    assert (layout["state"] / "state" / "current").read_text().strip() == NEW
    assert (layout["state"] / "state" / "previous").read_text().strip() == BASELINE
    assert_persistent(layout["live"])
    calls = (layout["state"] / "pm2-calls").read_text().splitlines()
    assert calls
    for line in calls:
        assert " all" not in f" {line} "
        assert "empowerx-frontend" not in line
        assert "empowerx-facility" not in line
        assert "empowerx-api " not in f"{line} "
        parts = line.split()
        if parts and parts[0] in {"delete", "restart", "reload", "stop"}:
            assert parts[1] in {"empowerx-ai-api", "empowerx-ai-worker"}
    runtime_calls = (layout["state"] / "stub-runtime" / "calls").read_text()
    assert "check-database" in runtime_calls
    assert "check-worker" in runtime_calls
    assert "apply " not in runtime_calls
    assert_clean_log(result.stderr)
    log_text = "".join(path.read_text() for path in (layout["state"] / "logs").glob("*.log"))
    assert_clean_log(log_text)


def test_pending_migration_is_not_automatic(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stage_bundle(layout["state"], NEW, "v2")
    stub = layout["state"] / "stub-runtime"
    (stub / "migrations.exit").write_text("10\n")
    (stub / "migrations.out").write_text("current=keep\nhead=next_rev\npending=next_rev\n")
    blocked = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert blocked.returncode == 6
    assert "explicit approval" in blocked.stderr
    assert not (layout["live"] / "app").is_symlink()
    calls = (stub / "calls").read_text() if (stub / "calls").exists() else ""
    assert "apply " not in calls

    (layout["state"] / "allow-migrations").write_text("different\n")
    layout["env"]["EMPOWERX_AI_APPROVAL_FILE"] = str(layout["state"] / "allow-migrations")
    (stub / "head").write_text("next_rev\n")
    mismatched = run("deploy/bin/deploy.sh", [NEW, "migrate=yes"], layout["env"])
    assert mismatched.returncode == 6
    assert (layout["state"] / "allow-migrations").exists()

    (layout["state"] / "allow-migrations").write_text("next_rev\n")
    (layout["state"] / "incoming" / f"{NEW}.tar.gz").unlink()
    stage_bundle(layout["state"], NEW, "v2")
    # The failed attempt removed the release directory. Re-stage and approve.
    approved = run("deploy/bin/deploy.sh", [NEW, "migrate=yes"], layout["env"])
    assert approved.returncode == 0, approved.stderr
    assert "apply " in (stub / "calls").read_text()
    assert not (layout["state"] / "allow-migrations").exists()
    assert "downgrade" not in (stub / "calls").read_text()
    assert_persistent(layout["live"])


def test_manual_rollback_does_not_touch_database_commands(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    stage_bundle(layout["state"], NEW, "v2")
    deployed = run("deploy/bin/deploy.sh", [NEW], layout["env"])
    assert deployed.returncode == 0, deployed.stderr
    rolled = run("deploy/bin/rollback.sh", [], layout["env"])
    assert rolled.returncode == 0, rolled.stderr
    assert (layout["live"] / "app" / "VERSION").read_text() == "v1\n"
    assert_persistent(layout["live"])
    script = (REPO / "deploy" / "bin" / "rollback.sh").read_text()
    migrate = (REPO / "deploy" / "vps" / "empowerx-ai-migrate").read_text()
    assert "pg_restore" not in script
    assert "downgrade" not in script
    assert "pg_restore" not in migrate
    joined = script + migrate + rolled.stderr
    assert "supersecret" not in joined


def test_baseline_backup_excludes_secrets_and_data(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    backup = layout["state"] / "backups" / f"baseline-{BASELINE}"
    assert (backup / "app" / "VERSION").read_text() == "v1\n"
    assert not (backup / ".env").exists()
    assert not (backup / "uploads").exists()
    assert not (backup / "preprocessed").exists()
    assert not (backup / ".venv").exists()
    tree = "\n".join(path.read_text(errors="ignore") for path in backup.rglob("*") if path.is_file())
    assert SECRET not in tree
    manifest = (backup / "MANIFEST").read_text()
    assert "database" in manifest
    assert SECRET not in manifest


def test_prune_keeps_baseline_current_and_previous(tmp_path: Path) -> None:
    state = tmp_path / "state"
    releases = state / "releases"
    releases.mkdir(parents=True)
    keep = {"current": "c" * 40, "previous": "d" * 40, "baseline": "e" * 40}
    (state / "state").mkdir()
    for name, sha in keep.items():
        (state / "state" / name).write_text(sha + "\n")
        (releases / sha).mkdir()
    extras = []
    for index in range(8):
        sha = f"{index:040x}"
        path = releases / sha
        path.mkdir()
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
        extras.append(sha)
    env = os.environ.copy()
    env["EMPOWERX_AI_STATE_DIR"] = str(state)
    script = "source '{0}/deploy/lib/release.sh'; prune_releases".format(REPO)
    result = subprocess.run(["bash", "-lc", script], env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    remaining = {path.name for path in releases.iterdir()}
    assert set(keep.values()) <= remaining
    # Newest five extras use the highest mtimes and stay.
    assert set(extras[-5:]) <= remaining
    assert extras[0] not in remaining


def test_receive_and_remote_command_are_restricted(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    payload, digest = bundle_bytes(NEW, "v2")
    bundle = tmp_path / "bundle.tar.gz"
    bundle.write_bytes(payload)
    proc = subprocess.run(
        ["bash", str(REPO / "deploy/bin/receive-artifact.sh"), NEW],
        input=(digest + "\n").encode() + payload,
        env=layout["env"],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    stored = layout["state"] / "incoming" / f"{NEW}.tar.gz"
    assert stored.read_bytes() == payload

    rejected = subprocess.run(
        ["bash", str(REPO / "deploy/vps/empowerx-ai-deploy-shell")],
        env={**layout["env"], "EMPOWERX_AI_ORIGINAL_COMMAND": "deploy $(uname) ; rm -rf /"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2

    ssh_log = tmp_path / "ssh-log"
    write_exe(
        layout["state"] / "bin" / "ssh",
        textwrap.dedent(
            f"""\
            #!/bin/bash
            printf '%s\\n' "$*" >> "{ssh_log}"
            if [[ "$*" == *receive-artifact* ]]; then
              cat >/dev/null
            fi
            exit 0
            """
        ),
    )
    identity = tmp_path / "id"
    known = tmp_path / "known_hosts"
    identity.write_text("fake-key\n")
    identity.chmod(0o600)
    known.write_text("srv2028039.hstgr.cloud ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample\n")
    remote_env = layout["env"].copy()
    remote_env.update(
        {
            "EMPOWERX_AI_SSH": str(layout["state"] / "bin" / "ssh"),
            "EMPOWERX_AI_SSH_IDENTITY": str(identity),
            "EMPOWERX_AI_KNOWN_HOSTS": str(known),
            "EMPOWERX_AI_SKIP_PACKAGE": "1",
            "EMPOWERX_AI_ARTIFACT": str(bundle),
            "VPS_HOST": "srv2028039.hstgr.cloud",
            "VPS_USER": "empowerx-ai-deploy",
            "VPS_PORT": "22",
            "GITHUB_ACTIONS": "",
        }
    )
    remote_env.pop("GITHUB_ACTIONS", None)
    remote = run("deploy/bin/remote-deploy.sh", [NEW], remote_env)
    assert remote.returncode == 0, remote.stderr
    logged = ssh_log.read_text()
    assert "StrictHostKeyChecking=yes" in logged
    assert f"receive-artifact {NEW}" in logged
    assert f"deploy {NEW}" in logged
    assert "migrate=yes" not in logged
    assert ";" not in logged.split("receive-artifact", 1)[1].split("\n", 1)[0]


def test_install_dry_run_and_non_root_refusal(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", "empowerx-ai-deploy-test"],
        check=True,
        capture_output=True,
    )
    dry = subprocess.run(
        [
            "bash",
            str(REPO / "deploy/vps/install.sh"),
            "--dry-run",
            "--bundle",
            str(REPO / "deploy"),
            "--pubkey",
            str(key) + ".pub",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert dry.returncode == 0, dry.stderr
    assert "no system changes" in dry.stderr
    refused = subprocess.run(
        [
            "bash",
            str(REPO / "deploy/vps/install.sh"),
            "--bundle",
            str(REPO / "deploy"),
            "--pubkey",
            str(key) + ".pub",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert refused.returncode == 1
    assert "root" in refused.stderr


def test_protected_pm2_pid_change_aborts(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    (layout["state"] / "pm2-change-protected").write_text("1\n")
    result = run("deploy/vps/empowerx-ai-pm2", ["reload-ai"], layout["env"])
    assert result.returncode == 9
    calls = (layout["state"] / "pm2-calls").read_text()
    assert "empowerx-frontend" not in calls
    assert "empowerx-facility" not in calls
    assert " all" not in calls


def test_migration_scanner_and_env_parser(tmp_path: Path) -> None:
    scanner = REPO / "deploy/lib/scan_migrations.py"
    existing = subprocess.run(["python3", str(scanner), str(REPO / "migrations")], capture_output=True, text=True)
    assert existing.returncode == 0, existing.stderr
    bad = tmp_path / "bad.py"
    bad.write_text('op.drop_table("users")\n')
    rejected = subprocess.run(["python3", str(scanner), str(bad)], capture_output=True, text=True)
    assert rejected.returncode == 1
    import importlib.util

    spec = importlib.util.spec_from_file_location("migration_status", REPO / "deploy/lib/migration_status.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql+psycopg://app:supersecret-marker@127.0.0.1/empowerx\n")
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        values = module.load_env_file(env_file)
    assert values["DATABASE_URL"].endswith("/empowerx")
    assert "supersecret-marker" not in buffer.getvalue()


def test_workflow_and_ecosystem_policy() -> None:
    workflow = (REPO / ".github/workflows/ai-cicd.yml").read_text()
    assert "pull_request_target" not in workflow
    assert "environment: development" in workflow
    assert "VPS_SSH_PRIVATE_KEY" in workflow
    assert "VPS_KNOWN_HOSTS" in workflow
    assert "VPS_HOST" in workflow
    assert "VPS_USER" in workflow
    assert "VPS_PORT" in workflow
    assert "contents: read" in workflow
    assert "bud_development" in workflow
    assert "github.event_name == 'pull_request'" in workflow or "github.event_name == 'push'" in workflow
    assert "permissions:" in workflow
    ecosystem = (REPO / "deploy/vps/ecosystem.config.cjs").read_text()
    assert "root-path" not in ecosystem
    assert "127.0.0.1" in ecosystem
    assert "8002" in ecosystem
    assert "empowerx-ai-api" in ecosystem
    assert "empowerx-ai-worker" in ecosystem
    assert "-Q radiology,celery --concurrency=1" in ecosystem
    assert "empowerx-frontend" not in ecosystem
    assert "empowerx-facility" not in ecosystem
    sudoers = (REPO / "deploy/vps/sudoers-empowerx-ai-deploy").read_text()
    assert "requiretty" not in sudoers
    assert "empowerx-ai-deploy" in sudoers
    assert "empowerx-ai-pm2" in sudoers


def test_package_artifact_round_trip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "migrations" / "versions").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "VERSION").write_text("v9\n")
    (repo / "requirements.txt").write_text("# test\n")
    (repo / "alembic.ini").write_text("[alembic]\nscript_location = migrations\n")
    (repo / "pyproject.toml").write_text("[project]\nname='t'\nversion='0'\n")
    (repo / "migrations" / "versions" / "keep.py").write_text("revision = 'keep'\n")
    subprocess.run(["git", "init", "-b", "bud_development"], cwd=repo, check=True, capture_output=True)
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
    env["EMPOWERX_AI_REPO"] = str(repo)
    result = subprocess.run(
        ["bash", str(REPO / "deploy/bin/package-artifact.sh"), sha, str(out)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert (tmp_path / "out.tar.gz.sha256").read_text().strip()
