#!/usr/bin/env python3
"""Replace a symlink without following it.

macOS mv follows a symlink that points at a directory. os.replace updates the
directory entry itself, which is what a release switch needs.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: atomic_symlink.py TARGET LINKPATH\n")
        return 2
    target, linkpath = sys.argv[1], sys.argv[2]
    parent = os.path.dirname(linkpath) or "."
    if not os.path.isdir(parent):
        sys.stderr.write("link parent is not a directory\n")
        return 1
    if os.path.lexists(linkpath) and not os.path.islink(linkpath):
        sys.stderr.write("refusing to replace a non-symlink\n")
        return 1
    tmp = os.path.join(
        parent,
        ".ln.{0}.{1}".format(os.getpid(), os.path.basename(linkpath)),
    )
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(target, tmp)
    try:
        os.replace(tmp, linkpath)
    except OSError as exc:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        sys.stderr.write("symlink replace failed: {0}\n".format(type(exc).__name__))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
