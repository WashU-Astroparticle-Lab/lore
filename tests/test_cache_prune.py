"""Bounded caches — figures fetched for viewing must not accumulate forever.

Nothing in the project deleted a cached figure. 13 MB of LabArchives images had
built up with no ceiling, inside a OneDrive-synced tree, and Slack attachment
downloads were about to do the same.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cache_prune import dir_size, plan_prune, prune

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


def _entry(root: Path, name: str, size_bytes: int, age_days: float) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "fig.png").write_bytes(b"x" * size_bytes)
    when = time.time() - age_days * 86400
    os.utime(d / "fig.png", (when, when))
    os.utime(d, (when, when))
    return d


def test_age_rule() -> None:
    root = Path(tempfile.mkdtemp())
    _entry(root, "old_page", 1000, age_days=30)
    _entry(root, "fresh_page", 1000, age_days=1)

    doomed = {p.name for p, _s, _r in plan_prune(root, max_age_days=14, max_mb=999)}
    check("a stale entry is selected", "old_page" in doomed)
    check("a recent entry is kept", "fresh_page" not in doomed)

    removed, freed = prune(root, max_age_days=14, max_mb=999, apply=True)
    check("prune removes exactly the stale entry", removed == 1 and freed >= 1000)
    check("the stale directory is gone", not (root / "old_page").exists())
    check("the fresh directory survives", (root / "fresh_page").exists())


def test_size_cap_beats_recency() -> None:
    """A burst of fetches in one day must not blow past the cap just by being new."""
    root = Path(tempfile.mkdtemp())
    for i in range(5):
        _entry(root, f"page_{i}", 2 * 1024 * 1024, age_days=i * 0.01)  # ~2 MB each, all fresh

    doomed = plan_prune(root, max_age_days=14, max_mb=6)
    check("the size cap still fires on all-recent entries", len(doomed) > 0)
    check("every eviction cites the cap", all("cap" in r for _p, _s, r in doomed))
    # Newest are kept: page_0 is the most recent.
    check("the newest entry is never evicted",
          "page_0" not in {p.name for p, _s, _r in doomed})

    prune(root, max_age_days=14, max_mb=6, apply=True)
    check("the directory ends up under the cap", dir_size(root) <= 6 * 1024 * 1024)


def test_dry_run_and_edges() -> None:
    root = Path(tempfile.mkdtemp())
    _entry(root, "old_page", 1000, age_days=30)
    removed, freed = prune(root, max_age_days=14, max_mb=999, apply=False)
    check("dry run reports without deleting", removed == 1 and (root / "old_page").exists())

    check("a missing directory is not an error", plan_prune(Path(tempfile.mkdtemp()) / "nope") == [])
    check("an empty directory yields nothing", plan_prune(Path(tempfile.mkdtemp())) == [])


if __name__ == "__main__":
    test_age_rule()
    test_size_cap_beats_recency()
    test_dry_run_and_edges()
    print(f"\ntest_cache_prune: {PASSED} checks passed")
