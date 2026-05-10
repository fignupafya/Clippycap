"""Recursive filesystem walking for library scans.

Yields every *file* under a root directory as an absolute, resolved :class:`~pathlib.Path`,
honouring the scan options (recursion, symlink following, hidden files, ignored glob patterns).
Directory loops are avoided by tracking visited real paths; unreadable entries are skipped.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from fnmatch import fnmatch
from pathlib import Path


def _ignored(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch(name, pat) for pat in patterns)


def walk_files(
    root: Path,
    *,
    recursive: bool,
    follow_symlinks: bool,
    include_hidden: bool,
    ignored_globs: Sequence[str],
) -> Iterator[Path]:
    root = root.resolve()
    if not root.is_dir():
        return
    visited: set[str] = set()
    stack: list[Path] = [root]
    while stack:
        directory = stack.pop()
        try:
            real = os.path.realpath(directory)
        except OSError:
            continue
        if real in visited:
            continue
        visited.add(real)
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if not include_hidden and name.startswith("."):
                continue
            if _ignored(name, ignored_globs):
                continue
            try:
                if entry.is_dir(follow_symlinks=follow_symlinks):
                    if recursive:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=follow_symlinks):
                    yield Path(entry.path).resolve()
            except OSError:
                continue
