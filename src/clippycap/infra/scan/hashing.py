"""Content-identity strategies for the scanner.

:class:`Blake3IdentityStrategy` hashes the whole file with BLAKE3.
:class:`Blake3CompositeIdentityStrategy` hashes only the file's exact byte size plus a slice taken
from each end -- far less disk I/O, and for real media files just as collision-free (the size is
part of the hash, so two files share an identity only if their byte count *and* both ends match).

Both pair with the ``(path, size, mtime)`` cache in the database, so a re-scan of unchanged files
re-hashes nothing. Plugins may register others (for example a perceptual video hash that survives
re-encoding or trimming).
"""

from __future__ import annotations

from pathlib import Path

import blake3

_CHUNK_SIZE = 1 << 20  # 1 MiB -- the streaming read size when hashing a file in full

# Every identity string is algorithm-prefixed; the prefix lets the rest of the app tell which
# strategy produced a stored hash (so e.g. a strategy change can re-identify the old library).
_FULL_PREFIX = "b3:"
_COMPOSITE_PREFIX = "b3c:"


class Blake3IdentityStrategy:
    """Full-content BLAKE3 hash. Identity strings are prefixed ``b3:`` (e.g. ``b3:9f4c...``)."""

    name = "blake3"
    identity_prefix = _FULL_PREFIX

    def compute(self, path: Path, size: int) -> str:
        del size  # this strategy reads the whole file, so it does not need the size hint
        hasher = blake3.blake3()
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                hasher.update(chunk)
        return f"{_FULL_PREFIX}{hasher.hexdigest()}"


class Blake3CompositeIdentityStrategy:
    """A composite BLAKE3 hash over the file's exact size plus a head and tail slice.

    Reads only ``head_bytes + tail_bytes`` per file instead of all of it, so scanning a large
    library is many times faster -- while staying collision-free for real media: the file's exact
    size is the first thing fed to the hasher, so two files share an identity only if their byte
    count *and* both end slices are identical. Identity strings are prefixed ``b3c:``.

    If the file is no larger than ``head_bytes + tail_bytes`` (or both are zero) the whole file is
    hashed instead -- so the head and tail slices never overlap ambiguously, and tiny files stay
    exact. The result is fully deterministic for a given (size, file content).
    """

    name = "blake3-composite"
    identity_prefix = _COMPOSITE_PREFIX

    def __init__(self, *, head_bytes: int, tail_bytes: int) -> None:
        self._head = max(0, head_bytes)
        self._tail = max(0, tail_bytes)

    def compute(self, path: Path, size: int) -> str:
        hasher = blake3.blake3()
        hasher.update(size.to_bytes(8, "little", signed=False))   # the exact size IS part of the identity
        slice_total = self._head + self._tail
        with path.open("rb") as handle:
            if slice_total == 0 or size <= slice_total:
                # no slices configured, or the file is small enough that head+tail would overlap:
                # hash every byte (still cheap -- the file is small) so the identity stays exact.
                while chunk := handle.read(_CHUNK_SIZE):
                    hasher.update(chunk)
            else:
                # a buffered file read of N bytes returns exactly N here (the file is larger than
                # both slices combined), so the head and tail are captured in full.
                if self._head:
                    hasher.update(handle.read(self._head))
                if self._tail:
                    handle.seek(size - self._tail)
                    hasher.update(handle.read(self._tail))
        return f"{_COMPOSITE_PREFIX}{hasher.hexdigest()}"
