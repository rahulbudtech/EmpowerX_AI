#!/usr/bin/env python3
"""Read Alembic status and apply an approved upgrade without printing secrets."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DUMP_TABLES = ("radiology_findings", "radiology_token_usage", "alembic_version")


def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines. Do not interpolate or log values."""
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ValueError("invalid env line")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("invalid env key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "$(" in value or "`" in value:
            raise ValueError("unsupported env substitution")
        values[key] = value
    return values


def _fail(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    return 1


def collect_status(release: Path, env: dict[str, str]) -> tuple[list[str], str, list[str]]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlalchemy.engine.url import make_url

    url = make_url(env["DATABASE_URL"])
    if not str(url.drivername).startswith("postgresql"):
        raise RuntimeError("driver")
    config = Config(str(release / "alembic.ini"))
    config.set_main_option("script_location", str(release / "migrations"))
    script = ScriptDirectory.from_config(config)
    heads = list(script.get_heads())
    if len(heads) != 1:
        raise RuntimeError("heads")
    known = {revision.revision for revision in script.walk_revisions()}
    engine = create_engine(env["DATABASE_URL"], pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            current = list(context.get_current_heads())
    finally:
        engine.dispose()
    for revision in current:
        if revision not in known:
            raise RuntimeError("foreign-revision")
    head = heads[0]
    pending: list[str] = []
    if not current:
        pending = [revision.revision for revision in script.walk_revisions()]
    elif head not in current:
        lower = current[0]
        for revision in script.iterate_revisions(head, lower):
            if revision.revision != lower:
                pending.append(revision.revision)
    return current, head, pending


def _print_status(current: list[str], head: str, pending: list[str]) -> int:
    sys.stdout.write("current={0}\n".format(",".join(current) if current else "none"))
    sys.stdout.write("head={0}\n".format(head))
    if not pending:
        sys.stdout.write("pending=none\n")
        return 0
    for revision in pending:
        sys.stdout.write("pending={0}\n".format(revision))
    return 10


def _status(release: Path, env_file: Path) -> int:
    try:
        env = load_env_file(env_file)
        if "DATABASE_URL" not in env:
            return _fail("migration status failed: env")
        os.environ.update(env)
        current, head, pending = collect_status(release, env)
    except Exception as exc:
        return _fail("migration status failed: {0}".format(type(exc).__name__))
    return _print_status(current, head, pending)


def _backup_and_upgrade(release: Path, env_file: Path, backup: Path, expect_head: str) -> int:
    if not re.fullmatch(r"[A-Za-z0-9_]+", expect_head):
        return _fail("migration upgrade refused")
    try:
        env = load_env_file(env_file)
        if "DATABASE_URL" not in env:
            return _fail("migration upgrade failed: env")
        os.environ.update(env)
        current, head, pending = collect_status(release, env)
        _print_status(current, head, pending)
        if head != expect_head:
            return _fail("migration upgrade refused")
        if not pending:
            return 0
        from sqlalchemy.engine.url import make_url

        url = make_url(env["DATABASE_URL"])
        if not url.username or not url.database:
            return _fail("migration backup failed")
        backup.parent.mkdir(parents=True, exist_ok=True)
        dump_env = os.environ.copy()
        if url.password:
            dump_env["PGPASSWORD"] = url.password
        pg_dump = os.environ.get("EMPOWERX_AI_PG_DUMP", "pg_dump")
        command = [
            pg_dump,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
        ]
        for table in DUMP_TABLES:
            command.extend(["--table", table])
        command.extend(
            [
                "--file",
                str(backup),
                "--host",
                url.host or "127.0.0.1",
                "--port",
                str(url.port or 5432),
                "--username",
                url.username,
                "--dbname",
                url.database,
            ]
        )
        completed = subprocess.run(command, env=dump_env, check=False, capture_output=True)
        if completed.returncode != 0:
            return _fail("migration backup failed")
        os.chmod(backup, 0o600)
        alembic = str(Path(sys.executable).with_name("alembic"))
        upgraded = subprocess.run(
            [alembic, "-c", str(release / "alembic.ini"), "upgrade", "head"],
            cwd=str(release),
            env=os.environ.copy(),
            check=False,
            capture_output=True,
        )
        if upgraded.returncode != 0:
            return _fail("migration upgrade failed")
        _, _, pending_after = collect_status(release, env)
        if pending_after:
            return _fail("migration upgrade failed")
    except Exception as exc:
        return _fail("migration upgrade failed: {0}".format(type(exc).__name__))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="EmpowerX AI migration helper")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--release", required=True)
    check.add_argument("--env-file", required=True)
    upgrade = sub.add_parser("upgrade")
    upgrade.add_argument("--release", required=True)
    upgrade.add_argument("--env-file", required=True)
    upgrade.add_argument("--backup", required=True)
    upgrade.add_argument("--expect-head", required=True)
    args = parser.parse_args()
    if args.command == "check":
        return _status(Path(args.release), Path(args.env_file))
    return _backup_and_upgrade(
        Path(args.release),
        Path(args.env_file),
        Path(args.backup),
        args.expect_head,
    )


if __name__ == "__main__":
    raise SystemExit(main())
