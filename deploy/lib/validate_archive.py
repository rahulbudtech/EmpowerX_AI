#!/usr/bin/env python3
"""Reject unsafe deployment archives and extract only regular files."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

MAX_MEMBER_BYTES = 80 * 1024 * 1024
MAX_TOTAL_BYTES = 400 * 1024 * 1024
REQUIRED_RELATIVE = (
    "app",
    "requirements.txt",
    "alembic.ini",
    "migrations",
    "pyproject.toml",
)


class ArchiveRejected(Exception):
    """The archive is not safe to extract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parts(name: str) -> list[str]:
    normalized = name.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.endswith("/"):
        normalized = normalized[:-1]
    if not normalized:
        raise ArchiveRejected("empty archive path")
    if normalized.startswith("/"):
        raise ArchiveRejected("absolute archive path")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ArchiveRejected("path traversal in archive")
    for part in parts:
        if "\x00" in part or "\n" in part or "\r" in part:
            raise ArchiveRejected("illegal character in archive path")
    top = parts[0]
    if top in {"uploads", "preprocessed", ".venv", ".git"}:
        raise ArchiveRejected("archive contains a persistent or vcs path")
    if top == ".env" or (top.startswith(".env.") and top != ".env.example"):
        raise ArchiveRejected("archive contains an environment file")
    return parts


def _reject_member(member: tarfile.TarInfo) -> list[str]:
    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
        raise ArchiveRejected("refusing special archive member")
    if not (member.isfile() or member.isdir()):
        raise ArchiveRejected("refusing unsupported archive member")
    parts = _parts(member.name)
    if member.isfile() and member.size > MAX_MEMBER_BYTES:
        raise ArchiveRejected("archive member exceeds size limit")
    return parts


def _safe_destination(root: Path, parts: list[str]) -> Path:
    candidate = root.joinpath(*parts)
    if candidate != root and root not in candidate.parents:
        raise ArchiveRejected("archive path escapes destination")
    return candidate


def extract_tar(archive: Path, destination: Path) -> None:
    """Extract regular files and directories after a full member scan."""
    if destination.exists():
        raise ArchiveRejected("destination already exists")
    total = 0
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = tar.getmembers()
            for member in members:
                _reject_member(member)
                if member.isfile():
                    total += member.size
            if total > MAX_TOTAL_BYTES:
                raise ArchiveRejected("archive exceeds total size limit")
            parent = destination.parent
            parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=".extract.", dir=str(parent)))
            try:
                for member in members:
                    parts = _parts(member.name)
                    target = _safe_destination(tmp, parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        raise ArchiveRejected("missing archive member payload")
                    with source, target.open("wb") as handle:
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            handle.write(chunk)
                            remaining -= len(chunk)
                    os.chmod(target, 0o644)
                for dirpath, dirnames, filenames in os.walk(tmp):
                    os.chmod(dirpath, 0o755)
                    for filename in filenames:
                        os.chmod(Path(dirpath) / filename, 0o644)
                    for dirname in dirnames:
                        path = Path(dirpath) / dirname
                        if path.is_symlink():
                            raise ArchiveRejected("extracted symlink")
                if any(Path(dirpath).is_symlink() or (Path(dirpath) / name).is_symlink()
                       for dirpath, dirnames, filenames in os.walk(tmp)
                       for name in dirnames + filenames):
                    raise ArchiveRejected("extracted symlink")
                os.rename(tmp, destination)
                tmp = None
            finally:
                if tmp is not None and tmp.exists():
                    shutil.rmtree(tmp)
    except tarfile.TarError as exc:
        raise ArchiveRejected("malformed archive") from exc


def _read_manifest(path: Path) -> dict[str, str]:
    allowed = {"commit", "archive_sha256", "branch", "created_at"}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            raise ArchiveRejected("malformed manifest")
        key, value = line.split("=", 1)
        if key not in allowed or key in found:
            raise ArchiveRejected("malformed manifest")
        if not value or any(char in value for char in "\n\r$`"):
            raise ArchiveRejected("malformed manifest")
        found[key] = value
    missing = allowed - set(found)
    if missing:
        raise ArchiveRejected("malformed manifest")
    return found


def _require_application_tree(root: Path) -> None:
    for relative in REQUIRED_RELATIVE:
        if not (root / relative).exists():
            raise ArchiveRejected("archive is missing application files")


def deploy_bundle(
    archive: Path,
    checksum: str,
    expected_commit: str,
    destination: Path,
    require_branch: str | None,
) -> None:
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ArchiveRejected("malformed checksum")
    actual = sha256_file(archive)
    if not hmac.compare_digest(actual, checksum):
        raise ArchiveRejected("checksum mismatch")
    if len(expected_commit) != 40 or any(char not in "0123456789abcdef" for char in expected_commit):
        raise ArchiveRejected("invalid commit sha")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    outer_root = Path(tempfile.mkdtemp(prefix=".bundle.", dir=str(parent)))
    outer_tmp = outer_root / "contents"
    try:
        extract_tar(archive, outer_tmp)
        names = sorted(path.name for path in outer_tmp.iterdir())
        if names != ["manifest.txt", "source.tar.gz"]:
            raise ArchiveRejected("unexpected bundle contents")
        manifest = _read_manifest(outer_tmp / "manifest.txt")
        if manifest["commit"] != expected_commit:
            raise ArchiveRejected("commit does not match artifact")
        if require_branch and manifest["branch"] != require_branch:
            raise ArchiveRejected("artifact branch is not deployable")
        inner = outer_tmp / "source.tar.gz"
        inner_sha = sha256_file(inner)
        if not hmac.compare_digest(inner_sha, manifest["archive_sha256"]):
            raise ArchiveRejected("inner archive checksum mismatch")
        extract_tar(inner, destination)
        _require_application_tree(destination)
    finally:
        if outer_root.exists():
            shutil.rmtree(outer_root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an EmpowerX AI deploy archive")
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract")
    extract.add_argument("--archive", required=True)
    extract.add_argument("--dest", required=True)

    bundle = sub.add_parser("deploy-bundle")
    bundle.add_argument("--archive", required=True)
    bundle.add_argument("--checksum", required=True)
    bundle.add_argument("--expected-commit", required=True)
    bundle.add_argument("--dest", required=True)
    bundle.add_argument("--require-branch", default="")

    args = parser.parse_args()
    try:
        if args.command == "extract":
            extract_tar(Path(args.archive), Path(args.dest))
        else:
            deploy_bundle(
                Path(args.archive),
                args.checksum.strip(),
                args.expected_commit.strip(),
                Path(args.dest),
                args.require_branch or None,
            )
    except ArchiveRejected as exc:
        sys.stderr.write("artifact rejected: {0}\n".format(exc))
        return 3
    except OSError as exc:
        sys.stderr.write("artifact unreadable: {0}\n".format(type(exc).__name__))
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
