#!/usr/bin/env python3
"""Reject Alembic revisions that touch anything except EmpowerX AI tables."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ALLOWED_TABLES = {"radiology_findings", "alembic_version"}
OPERATION_RE = re.compile(
    r"op\.(drop_table|drop_column|alter_column|add_column|create_table|rename_table)"
    r"\s*\(\s*[\"']([A-Za-z0-9_\.]+)[\"']",
    re.S,
)
RAW_SQL_RE = re.compile(
    r"\b(drop\s+database|drop\s+schema|drop\s+table|truncate(?:\s+table)?|"
    r"delete\s+from|alter\s+table|update)\s+"
    r"(?:if\s+exists\s+)?(?:only\s+)?([A-Za-z0-9_\.\"]+)",
    re.I,
)


def _bare_table(name: str) -> str:
    cleaned = name.replace('"', "").replace("`", "")
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    return cleaned


def _strip_hash_comments(source: str) -> str:
    return re.sub(r"(?m)#.*$", "", source)


def scan_source(source: str, label: str) -> list[str]:
    problems: list[str] = []
    stripped = _strip_hash_comments(source)
    for match in OPERATION_RE.finditer(stripped):
        table = _bare_table(match.group(2))
        if table not in ALLOWED_TABLES:
            problems.append(
                "{0}: {1} targets {2}".format(label, match.group(1), table)
            )
    for match in RAW_SQL_RE.finditer(stripped):
        keyword = re.sub(r"\s+", " ", match.group(1).lower())
        if keyword in {"drop database", "drop schema"}:
            problems.append("{0}: raw {1}".format(label, keyword))
            continue
        table = _bare_table(match.group(2))
        if table not in ALLOWED_TABLES:
            problems.append("{0}: raw SQL targets {1}".format(label, table))
    return problems


def scan_tree(path: Path) -> list[str]:
    if not path.exists():
        return []
    problems: list[str] = []
    files = [path] if path.is_file() else sorted(path.rglob("*.py"))
    for file_path in files:
        if file_path.name == "__init__.py":
            continue
        text = file_path.read_text(encoding="utf-8")
        problems.extend(scan_source(text, str(file_path)))
    return problems


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: scan_migrations.py MIGRATIONS_DIR\n")
        return 2
    problems = scan_tree(Path(sys.argv[1]))
    if problems:
        sys.stderr.write("unsafe migration rejected\n")
        for problem in problems:
            sys.stderr.write("{0}\n".format(problem))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
