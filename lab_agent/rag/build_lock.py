"""Cross-process lock so only one knowledge-graph build runs at a time.

Two independent triggers can launch a KG refresh: the Slack listener's nightly
thread (``slack/sessions.py``) and the standalone Task Scheduler job
(``LORE-KG-nightly`` → ``build_kb --index``). LightRAG writes the KB storage dir
with atomic ``.tmp``→rename operations and is **not** safe to run concurrently —
two builders at once corrupt the graph. So every KG build acquires this lock
first; a second builder that finds it held simply skips its run. That is what
makes "keep both the listener job and the scheduler job" safe: whichever starts
first wins, the other backs off, and the manifest makes the skipped run's work
near-free whenever it next gets the lock.

The lock is a single file in the KB storage dir (so it lives beside the graph it
guards, out of OneDrive). A lock left behind by a crashed builder is reclaimed
once it is older than ``STALE_SECONDS``.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from ..config import kb_dir

# A KG build should never legitimately run longer than this; a lock older than
# STALE_SECONDS is assumed to belong to a dead process and is reclaimed. Generous
# enough to cover a manual `--full` rebuild of the whole notebook.
STALE_SECONDS = 6 * 60 * 60


class BuildLockHeld(Exception):
    """Raised when another process already holds the KG build lock."""


def _lock_path() -> Path:
    return kb_dir() / "kb_build.lock"


@contextmanager
def build_lock():
    """Hold the exclusive KG-build lock for the duration of the ``with`` block.

    Raises :class:`BuildLockHeld` immediately if another live build owns it.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Reclaim a stale lock left behind by a crashed/killed builder.
    try:
        if time.time() - path.stat().st_mtime > STALE_SECONDS:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise BuildLockHeld(f"another KG build already holds {path}") from exc

    try:
        os.write(fd, f"pid={os.getpid()} started={int(time.time())}\n".encode())
        os.close(fd)
        yield
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
