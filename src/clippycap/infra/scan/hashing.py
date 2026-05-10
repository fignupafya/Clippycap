"""Content-identity strategies for the scanner.

The built-in one hashes the whole file with BLAKE3; combined with the ``(path, size, mtime)``
cache in the database, a re-scan of unchanged files never re-hashes them. Plugins may register
others (for example a perceptual video hash that survives re-encoding or trimming).
"""

from __future__ import annotations

from pathlib import Path

import blake3

_CHUNK_SIZE = 1 << 20  # 1 MiB


class Blake3IdentityStrategy:
    """Full-content BLAKE3 hash. Identity strings are prefixed ``b3:`` (e.g. ``b3:9f4c...``)."""

    name = "blake3"

    def compute(self, path: Path, size: int) -> str:
        del size  # this strategy does not need the size hint
        hasher = blake3.blake3()
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                hasher.update(chunk)
        return f"b3:{hasher.hexdigest()}"
