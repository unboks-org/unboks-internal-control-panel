from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterator

try:  # pragma: no cover - fcntl is available on Linux/macOS.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


@contextlib.contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Small cross-process lock for JSON state files.

    Nr3 still uses flat files for the thin-control v0. This lock keeps
    concurrent web workers, double submits, and the host provisioner from
    racing while reading/writing those files.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def shared_file_lock(path: Path) -> Iterator[None]:
    """Allow concurrent readers while fencing writers on the same lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
