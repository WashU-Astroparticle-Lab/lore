"""
Bounded on-disk caches.

Fetched figures (LabArchives pages, Slack attachments) have to be written to
disk before anything can look at them — the Read tool needs a path, and Slack's
url_private needs an auth header, so neither can be read in place. Nothing used
to remove them again, so every fetch was permanent: 13 MB of LabArchives figures
had accumulated with no upper bound, inside a OneDrive-synced tree.

These caches are *reconstructible* — a re-fetch costs seconds — so they get an
age and a size ceiling. Experiment outputs and the crawled corpus are NOT touched
here: they are the record, not a cache.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

# Reconstructible caches only. Each is (path, max age in days, max size in MB).
DEFAULT_MAX_AGE_DAYS = 14
DEFAULT_MAX_MB = 200


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _entries_newest_first(root: Path) -> list[tuple[Path, float, int]]:
    """Immediate children of *root* as (path, mtime, size), newest first."""
    out = []
    for child in root.iterdir():
        try:
            size = dir_size(child) if child.is_dir() else child.stat().st_size
            out.append((child, child.stat().st_mtime, size))
        except OSError:
            continue
    out.sort(key=lambda e: e[1], reverse=True)
    return out


def plan_prune(
    root: Path,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_mb: int = DEFAULT_MAX_MB,
) -> list[tuple[Path, int, str]]:
    """Return [(path, bytes, reason)] for entries that should go. Does not delete.

    Age first, then oldest-first until the directory is under the size ceiling —
    so a burst of fetches in one day cannot blow past the cap just because
    everything in it is recent.
    """
    if not root.exists():
        return []
    from .retention import in_active_use

    entries = _entries_newest_first(root)
    cutoff = time.time() - max_age_days * 86400
    doomed: list[tuple[Path, int, str]] = []

    keep = []
    for path, mtime, size in entries:
        # A page whose figures are under discussion must stay, however old the
        # files are — re-fetching mid-conversation needs a live cookie and costs
        # the user a Duo tap.
        if path.is_dir() and in_active_use(path):
            keep.append((path, time.time(), size))
            continue
        if mtime < cutoff:
            doomed.append((path, size, f"older than {max_age_days}d"))
        else:
            keep.append((path, mtime, size))

    budget = max_mb * 1024 * 1024
    running = 0
    for path, _mtime, size in keep:          # newest first
        running += size
        if running > budget:
            doomed.append((path, size, f"over {max_mb} MB cap"))
    return doomed


def prune(
    root: Path,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    max_mb: int = DEFAULT_MAX_MB,
    apply: bool = True,
) -> tuple[int, int]:
    """Prune *root*. Returns (entries removed, bytes freed)."""
    doomed = plan_prune(root, max_age_days, max_mb)
    freed = 0
    removed = 0
    for path, size, _reason in doomed:
        if not apply:
            freed += size
            removed += 1
            continue
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        except OSError:
            continue          # a locked file is not worth failing a fetch over
        freed += size
        removed += 1
    return removed, freed


def prune_quietly(root: Path, **kw) -> None:
    """Best-effort prune used after a fetch. Never raises, never blocks the caller."""
    try:
        removed, freed = prune(Path(root), **kw)
        if removed:
            print(f"[cache] pruned {removed} stale entr(ies) from {Path(root).name} "
                  f"({freed / 1e6:.1f} MB freed)")
    except Exception:
        pass
