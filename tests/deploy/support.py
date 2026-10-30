"""Helpers for deployment tests. Not collected as tests."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import subprocess
import tarfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASELINE = "a" * 40
NEW = "b" * 40
SECRET = "supersecret-marker"
REAL_PYTHON = os.environ.get("EMPOWERX_PYTHON", "")


def real_python() -> str:
    if REAL_PYTHON:
        return REAL_PYTHON
    found = subprocess.check_output(["bash", "-lc", "command -v python3.12"], text=True).strip()
    return found


def write_exe(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def gzip_bytes(payload: bytes) -> bytes:
    import gzip

    return gzip.compress(payload)


def tar_gz(members: list[tarfile.TarInfo], payloads: dict[str, bytes] | None = None) -> bytes:
    payloads = payloads or {}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member in members:
            data = payloads.get(member.name)
            if data is not None and member.isfile():
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            else:
                archive.addfile(member)
    return buffer.getvalue()


def regular(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    return info, data


def application_files(version: str, migration: str | None = None) -> list[tuple[tarfile.TarInfo, bytes]]:
    files = {
        "app/__init__.py": b"",
        "app/VERSION": (version + "\n").encode(),
        "requirements.txt": b"# test requirements\n",
        "alembic.ini": b"[alembic]\nscript_location = migrations\n",
        "pyproject.toml": b"[project]\nname = 'fixture'\nversion = '0'\n",
        "migrations/versions/keep.py": (
            migration
            or "revision = 'keep'\n"
        ).encode(),
    }
    return [regular(name, data) for name, data in files.items()]


def bundle_bytes(commit: str, version: str, migration: str | None = None, inner: bytes | None = None) -> tuple[bytes, str]:
    if inner is None:
        members = application_files(version, migration)
        infos = []
        payloads = {}
        for info, data in members:
            infos.append(info)
            payloads[info.name] = data
        inner = tar_gz(infos, payloads)
    inner_sha = hashlib.sha256(inner).hexdigest()
    manifest = (
        f"commit={commit}\n"
        f"archive_sha256={inner_sha}\n"
        "branch=bud_development\n"
        "created_at=2026-10-26T00:00:00Z\n"
    ).encode()
    manifest_info, _ = regular("manifest.txt", manifest)
    inner_info = tarfile.TarInfo("source.tar.gz")
    inner_info.size = len(inner)
    inner_info.mode = 0o644
    inner_info.type = tarfile.REGTYPE
    outer = tar_gz([manifest_info, inner_info], {"manifest.txt": manifest, "source.tar.gz": inner})
    return outer, hashlib.sha256(outer).hexdigest()


def stage_bundle(state: Path, commit: str, version: str, migration: str | None = None, inner: bytes | None = None) -> None:
    payload, digest = bundle_bytes(commit, version, migration, inner)
    incoming = state / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / f"{commit}.tar.gz").write_bytes(payload)
    (incoming / f"{commit}.sha256").write_text(digest + "\n")


def make_layout(tmp: Path) -> dict[str, Path]:
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
    real = real_python()
    write_exe(
        bin_dir / "python",
        textwrap.dedent(
            f"""\
            #!/bin/bash
            if [[ "${{1:-}}" == "-c" && "$2" == *version_info* ]]; then
              exit 0
            fi
            if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
              dest=""
              for arg in "$@"; do dest="$arg"; done
              mkdir -p "$dest/bin"
              printf '%s\\n' '#!/bin/bash' 'exit 0' > "$dest/bin/python"
              chmod +x "$dest/bin/python"
              exit 0
            fi
            exec {real} "$@"
            """
        ),
    )
    write_exe(
        bin_dir / "uv",
        textwrap.dedent(
            f"""\
            #!/bin/bash
            if [[ -f "{state}/fail-deps" ]]; then
              echo "simulated dependency installation failure" >&2
              exit 1
            fi
            exit 0
            """
        ),
    )
    write_exe(
        bin_dir / "curl",
        textwrap.dedent(
            f"""\
            #!/bin/bash
            max_time=1
            output=""
            while [[ $# -gt 0 ]]; do
              case "$1" in
                --max-time) max_time="$2"; shift 2 ;;
                --output) output="$2"; shift 2 ;;
                --write-out|--silent|--show-error) shift ;;
                *) shift ;;
              esac
            done
            mode="ok"
            [[ -f "{state}/curl-mode" ]] && mode="$(cat "{state}/curl-mode")"
            phase="${{EMPOWERX_AI_HEALTH_PHASE:-deploy}}"
            if [[ "$mode" == "timeout" && "$phase" == "deploy" ]]; then
              sleep "$max_time"
              exit 28
            fi
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
                n=0
                [[ -f "{state}/jlist-count" ]] && n="$(cat "{state}/jlist-count")"
                n=$((n + 1))
                printf '%s\\n' "$n" > "{state}/jlist-count"
                pid=4242
                if [[ -f "{state}/pm2-change-protected" && "$n" -gt 1 ]]; then
                  pid=9999
                fi
                printf '%s\\n' "[{{\\"name\\":\\"empowerx-ai-api\\",\\"pid\\":11}},{{\\"name\\":\\"empowerx-ai-worker\\",\\"pid\\":12}},{{\\"name\\":\\"empowerx-api\\",\\"pid\\":$pid}},{{\\"name\\":\\"empowerx-frontend\\",\\"pid\\":3000}},{{\\"name\\":\\"empowerx-facility\\",\\"pid\\":3001}}]"
                ;;
              describe|delete|start|save)
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
    (state / "stub-runtime" / "migrations.out").write_text("current=keep\nhead=keep\npending=none\n")
    (state / "stub-runtime" / "worker-mode").write_text("ok\n")
    (state / "stub-runtime" / "database-mode").write_text("ok\n")
    (state / "stub-runtime" / "head").write_text("keep\n")
    (state / "curl-mode").write_text("ok\n")
    (REPO / "deploy" / "vps" / "ecosystem.config.cjs").read_text()
    (state / "ecosystem.config.cjs").write_text((REPO / "deploy" / "vps" / "ecosystem.config.cjs").read_text())
    env = os.environ.copy()
    env.update(
        {
            "EMPOWERX_AI_TEST_MODE": "1",
            "EMPOWERX_AI_LIVE_DIR": str(live),
            "EMPOWERX_AI_STATE_DIR": str(state),
            "EMPOWERX_AI_PYTHON": str(bin_dir / "python"),
            "EMPOWERX_AI_UV": str(bin_dir / "uv"),
            "EMPOWERX_AI_CURL": str(bin_dir / "curl"),
            "EMPOWERX_PM2": str(bin_dir / "pm2"),
            "EMPOWERX_AI_ECOSYSTEM": str(state / "ecosystem.config.cjs"),
            "EMPOWERX_AI_REQUIRE_PUBLIC_HEALTH": "no",
            "EMPOWERX_AI_MIN_FREE_KB": "1",
            "EMPOWERX_AI_HEALTH_TIMEOUT": "2",
            "EMPOWERX_AI_HEALTH_INTERVAL": "0.2",
            "EMPOWERX_AI_CURL_MAX_TIME": "1",
            "EMPOWERX_AI_WORKER_CHECK_ATTEMPTS": "5",
            "EMPOWERX_AI_WORKER_CHECK_DELAY": "0",
            "EMPOWERX_AI_LOCK_TRIES": "30",
            "EMPOWERX_AI_BASELINE_SHA": BASELINE,
            "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
        }
    )
    backup = subprocess.run(
        ["bash", str(REPO / "deploy" / "bin" / "backup-baseline.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if backup.returncode != 0:
        raise RuntimeError(backup.stderr + backup.stdout)
    return {"live": live, "state": state, "env": env}


def run(script: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(REPO / script), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_persistent(live: Path) -> None:
    env_text = (live / ".env").read_text()
    assert SECRET in env_text
    assert not (live / ".env").is_symlink()
    assert (live / "uploads" / "keep.txt").read_text() == "upload-keep\n"
    assert not (live / "uploads").is_symlink()
    assert (live / "preprocessed" / "keep.txt").read_text() == "pre-keep\n"
    assert not (live / "preprocessed").is_symlink()


def assert_clean_log(text: str) -> None:
    assert SECRET not in text
    assert "postgresql+psycopg://app:" not in text
