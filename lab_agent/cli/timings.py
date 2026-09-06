"""Report a per-phase wall-clock timeline for a completed experiment output dir.

The report phases (A-D) run as LLM-spawned Agent-tool subagents, so they cannot be
timed inside Python like run.py's fetch stages. Instead this derives a timeline from
output-file mtimes (deterministic, no LLM reliance): each phase's marker-file
timestamp minus the previous phase's gives its wall-clock duration. Combine with the
`fetch_timings_sec` block run.py writes into metadata.json for the fetch breakdown.

Usage:  python -m lab_agent.cli.timings outputs/<experiment_id>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ordered (phase label, marker filename or glob) checkpoints. Only those present
# are shown, sorted by mtime, so partial runs still report a partial timeline.
_CHECKPOINTS = [
    ("run.py fetch (end)",   "metadata.json"),
    ("Phase A: github",      "extracted_github.md"),
    ("Phase A: labarchives", "extracted_labarchives.md"),
    ("Phase A: deps",        "extracted_deps.md"),
    ("Phase A: dr",          "extracted_dr.md"),
    ("Phase B: synthesis",   "connections.md"),
    ("Phase C: report",      "[[]UNSIGNED[]] *.md"),
    ("Phase D: critique",    "critique.md"),
]


def _mtime(out_dir: Path, pattern: str) -> float | None:
    if any(c in pattern for c in "*?["):
        matches = sorted(out_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
        return matches[-1].stat().st_mtime if matches else None
    p = out_dir / pattern
    return p.stat().st_mtime if p.exists() else None


def phase_timeline(out_dir: Path) -> list[tuple[str, float, float | None]]:
    """Return [(label, mtime, delta_from_prev|None)] for present checkpoints, time-ordered."""
    present = [
        (label, m)
        for label, pat in _CHECKPOINTS
        if (m := _mtime(out_dir, pat)) is not None
    ]
    present.sort(key=lambda x: x[1])
    out: list[tuple[str, float, float | None]] = []
    prev: float | None = None
    for label, m in present:
        out.append((label, m, None if prev is None else m - prev))
        prev = m
    return out


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    out_dir = Path(sys.argv[1])
    if not out_dir.is_dir():
        print(f"Not a directory: {out_dir}")
        sys.exit(1)

    meta_path = out_dir / "metadata.json"
    if meta_path.exists():
        try:
            ft = json.loads(meta_path.read_text(encoding="utf-8")).get("fetch_timings_sec")
        except Exception:
            ft = None
        if ft:
            print("run.py fetch stages (s):")
            for k, v in ft.items():
                print(f"  {k:22s} {v}")
            print()

    timeline = phase_timeline(out_dir)
    if not timeline:
        print("No phase checkpoint files found.")
        return
    print("Phase timeline (from file mtimes):")
    total = 0.0
    for label, _m, delta in timeline:
        if delta is None:
            print(f"  {label:24s} (start)")
        else:
            total += delta
            print(f"  {label:24s} +{delta:7.1f}s")
    print(f"  {'-' * 24}")
    print(f"  {'phases total':24s} {total:8.1f}s")


if __name__ == "__main__":
    main()
