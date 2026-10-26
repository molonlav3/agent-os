"""Verify a downloadable file once, not on every request.

Artifact and attachment downloads check that the stored material still hashes
to the digest it was saved under. A ticketed URL serves ``HEAD``, ranges and
thumbnails, so one preview can be several requests; hashing the whole file on
the event loop each time stalled every other connection on a large file. A
verdict is cached per path for as long as the file's size, mtime and inode
are unchanged, and the hashing itself runs in a worker thread.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from pathlib import Path

import anyio

_MAX_ENTRIES = 1024
_CHUNK = 1024 * 1024

Stamp = tuple[int, int, int]

_verified: OrderedDict[str, tuple[Stamp, str]] = OrderedDict()
_lock = threading.Lock()


def _stamp(path: Path) -> Stamp | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cached_digest(path: Path) -> str | None:
    """The digest verified for ``path`` while it is unchanged, else None."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    key = os.fspath(path)
    with _lock:
        entry = _verified.get(key)
        if entry is None or entry[0] != stamp:
            return None
        _verified.move_to_end(key)
        return entry[1]


async def file_digest(path: Path) -> str:
    """sha256 of ``path``, from the cache when the file is unchanged."""
    cached = cached_digest(path)
    if cached is not None:
        return cached
    stamp = _stamp(path)
    digest = await anyio.to_thread.run_sync(_sha256, path)
    if stamp is not None and _stamp(path) == stamp:
        with _lock:
            _verified[os.fspath(path)] = (stamp, digest)
            _verified.move_to_end(os.fspath(path))
            while len(_verified) > _MAX_ENTRIES:
                _verified.popitem(last=False)
    return digest


def clear() -> None:
    with _lock:
        _verified.clear()


__all__ = ["cached_digest", "clear", "file_digest"]
