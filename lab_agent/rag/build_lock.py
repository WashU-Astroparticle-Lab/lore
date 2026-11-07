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


def _pid_in(text: str) -> int | None:
    for part in text.split():
        if part.startswith("pid="):
            try:
                return int(part[4:])
            except ValueError:
                return None
    return None


def _pid_alive(pid: int) -> bool:
    """Whether process ``pid`` is running. Errs towards True when it cannot tell."""
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def build_lock():
    """Hold the exclusive KG-build lock for the duration of the ``with`` block.

    Raises :class:`BuildLockHeld` immediately if another live build owns it.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Reclaim a lock left behind by a crashed/killed builder: one whose process is gone, or
    # one older than STALE_SECONDS. Age alone meant a hard-killed build blocked every run
    # for six hours, each skip looking like success.
    try:
        held = path.read_text(encoding="utf-8", errors="replace")
        pid = _pid_in(held)
        if (pid is not None and not _pid_alive(pid)) or \
                time.time() - path.stat().st_mtime > STALE_SECONDS:
            # Only remove the lock we judged stale, not one another builder just took.
            if path.read_text(encoding="utf-8", errors="replace") == held:
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
