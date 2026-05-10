"""Tests for the BLAKE3 identity strategy and the filesystem walker."""

from __future__ import annotations

from pathlib import Path

from clippycap.infra.scan.hashing import Blake3IdentityStrategy
from clippycap.infra.scan.walker import walk_files

IGNORED = ["Thumbs.db", "*.tmp"]


def test_blake3_identity_is_stable_and_content_addressed(tmp_path: Path) -> None:
    strat = Blake3IdentityStrategy()
    assert strat.name == "blake3"
    a = tmp_path / "a.bin"
    a.write_bytes(b"hello world" * 1000)
    b = tmp_path / "b.bin"
    b.write_bytes(b"hello world" * 1000)   # identical content
    c = tmp_path / "c.bin"
    c.write_bytes(b"different")
    h_a = strat.compute(a, a.stat().st_size)
    assert h_a.startswith("b3:")
    assert h_a == strat.compute(a, a.stat().st_size)        # stable
    assert h_a == strat.compute(b, b.stat().st_size)        # same content -> same id
    assert h_a != strat.compute(c, c.stat().st_size)        # different content -> different id


def _make_tree(root: Path) -> None:
    (root / "clip1.mp4").write_text("x")
    (root / "clip2.mp4").write_text("x")
    (root / "Thumbs.db").write_text("x")          # ignored by glob
    (root / "scratch.tmp").write_text("x")        # ignored by glob
    (root / ".hidden.mp4").write_text("x")        # hidden
    sub = root / "session2"
    sub.mkdir()
    (sub / "clip3.mp4").write_text("x")
    hidden_dir = root / ".cache"
    hidden_dir.mkdir()
    (hidden_dir / "clip4.mp4").write_text("x")    # inside a hidden dir


def test_walker_recursive_default(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    found = {p.name for p in walk_files(
        tmp_path, recursive=True, follow_symlinks=False, include_hidden=False, ignored_globs=IGNORED,
    )}
    assert found == {"clip1.mp4", "clip2.mp4", "clip3.mp4"}


def test_walker_non_recursive(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    found = {p.name for p in walk_files(
        tmp_path, recursive=False, follow_symlinks=False, include_hidden=False, ignored_globs=IGNORED,
    )}
    assert found == {"clip1.mp4", "clip2.mp4"}


def test_walker_include_hidden(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    found = {p.name for p in walk_files(
        tmp_path, recursive=True, follow_symlinks=False, include_hidden=True, ignored_globs=IGNORED,
    )}
    assert found == {"clip1.mp4", "clip2.mp4", "clip3.mp4", ".hidden.mp4", "clip4.mp4"}


def test_walker_nonexistent_root_yields_nothing(tmp_path: Path) -> None:
    assert list(walk_files(
        tmp_path / "nope", recursive=True, follow_symlinks=False, include_hidden=False, ignored_globs=[],
    )) == []


def test_walker_yields_absolute_paths(tmp_path: Path) -> None:
    (tmp_path / "x.mp4").write_text("x")
    for p in walk_files(tmp_path, recursive=True, follow_symlinks=False, include_hidden=False, ignored_globs=[]):
        assert p.is_absolute()
