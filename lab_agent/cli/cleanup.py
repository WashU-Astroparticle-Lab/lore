"""
Report and reclaim LORE's disk footprint.

    python -m lab_agent.cli.cleanup                    # report only — deletes nothing
    python -m lab_agent.cli.cleanup --caches           # prune re-fetchable caches
    python -m lab_agent.cli.cleanup --images           # strip figures from uploaded runs
    python -m lab_agent.cli.cleanup --logs             # trim session logs
    python -m lab_agent.cli.cleanup --all              # all three
    python -m lab_agent.cli.cleanup --images --min-age-days 60   # be stricter

Dry run is the default. Every flag above is required to delete anything.

Policy lives in lab_agent/retention.py. In short: the record (reports,
extractions, critiques, provenance, metadata, corpus, knowledge bundle) is never
auto-deleted — it measured 1.0 MB against 242.5 MB of figures. Figures are
reconstructible and are reclaimed once a run's report is in LabArchives AND the
run has gone idle: anything read recently is left alone, because re-fetching a
figure that is under discussion costs a live cookie, a Duo tap and a wait.

This tree usually sits inside OneDrive, which syncs every byte regardless of
.gitignore, so trimming reduces sync traffic as well as disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..cache_prune import DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_MB, dir_size, plan_prune, prune
from ..config import KNOWLEDGE_ROOT, PROJECT_ROOT
from ..retention import (
    LEGACY_MIN_AGE_DAYS, MIN_IDLE_DAYS, classify_run, duplicate_stats, strip_images,
)

CACHES = [KNOWLEDGE_ROOT / "image_cache"]
OUTPUTS = PROJECT_ROOT / "outputs"
SESSION_LOGS = PROJECT_ROOT / "session_logs"
KEEP_LOGS = 30


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def _flag(args: list[str], name: str) -> bool:
    return name in args or "--all" in args


def _opt(args: list[str], name: str, default: int) -> int:
    return int(args[args.index(name) + 1]) if name in args else default


def main() -> None:
    args = sys.argv[1:]
    do_caches, do_images, do_logs = _flag(args, "--caches"), _flag(args, "--images"), _flag(args, "--logs")
    min_age = _opt(args, "--min-age-days", LEGACY_MIN_AGE_DAYS)
    any_apply = do_caches or do_images or do_logs

    print("[cleanup] " + ("APPLYING" if any_apply else "dry run — nothing will be deleted"))
    freed_total = 0

    # ---- experiment runs -------------------------------------------------
    print("\n=== outputs/ — figures go once the report is uploaded AND the run is idle ===")
    print(f"    a run read within {MIN_IDLE_DAYS} days is left alone however old it is: a figure")
    print("    under discussion must stay on disk so the next question is instant")
    runs = sorted(d for d in OUTPUTS.iterdir() if d.is_dir()) if OUTPUTS.exists() else []
    for run in runs:
        info = classify_run(run, min_age)
        mark = "RECLAIM" if info["reclaimable"] else "keep   "
        print(f"  {mark} {run.name:<44} {_mb(info['images_bytes']):>9} figures "
              f"+ {_mb(info['text_bytes'])} text")
        print(f"          {info['reason']}")
        if info["reclaimable"]:
            n, freed = strip_images(run, apply=do_images)
            freed_total += freed
            verb = "removed" if do_images else "would remove"
            print(f"          {verb} {n} figure(s), {_mb(freed)} — text and metadata kept")

    # ---- duplication ------------------------------------------------------
    if OUTPUTS.exists():
        dup = duplicate_stats(OUTPUTS)
        if dup["wasted_bytes"]:
            print(f"\n=== duplication across runs: {_mb(dup['wasted_bytes'])} of byte-identical "
                  f"copies ({dup['duplicated_hashes']} of {dup['unique']} unique figures) ===")
            for w in dup["worst"][:3]:
                print(f"  {w['name']} — {w['copies']}x {_mb(w['bytes_each'])} in: {', '.join(w['runs'])}")
            print("  Cause: several runs fetched the same LabArchives pages, each into its own\n"
                  "  directory. Stripping uploaded runs above reclaims most of this. The structural\n"
                  "  fix — one shared content-addressed figure store — is a design change, not a\n"
                  "  cleanup, so it is reported here rather than done silently.")

    # ---- re-fetchable caches ---------------------------------------------
    print("\n=== re-fetchable caches ===")
    for root in CACHES:
        if not root.exists():
            print(f"  {root.name:<20} (absent)")
            continue
        doomed = plan_prune(root, DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_MB)
        if do_caches:
            n, freed = prune(root, DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_MB, apply=True)
        else:
            n, freed = len(doomed), sum(d[1] for d in doomed)
        freed_total += freed
        verb = "removed" if do_caches else "would remove"
        print(f"  {root.name:<20} {_mb(dir_size(root)):>9}  ->  {verb} {n} entr(ies), {_mb(freed)}")

    # ---- session logs -----------------------------------------------------
    print("\n=== session logs ===")
    if SESSION_LOGS.exists():
        logs = sorted(SESSION_LOGS.glob("session_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        stale = logs[KEEP_LOGS:]
        freed = sum(f.stat().st_size for f in stale)
        if do_logs:
            for f in stale:
                try:
                    f.unlink()
                except OSError:
                    pass
        freed_total += freed
        verb = "removed" if do_logs else "would remove"
        print(f"  {len(logs)} log(s); keeping the newest {KEEP_LOGS} — {verb} {len(stale)}, {_mb(freed)}")
    else:
        print("  (absent)")

    # ---- never touched ----------------------------------------------------
    print("\n=== the record — never auto-deleted ===")
    for root, what in [(KNOWLEDGE_ROOT / "labarchives", "crawled corpus (the KG source)"),
                       (KNOWLEDGE_ROOT / "experiments", "knowledge bundle"),
                       (PROJECT_ROOT / "cache", "small rebuildable caches")]:
        print(f"  {root.name:<20} {_mb(dir_size(root)) if root.exists() else '0.0 MB':>9}  {what}")
    print(f"  {'outputs/ text':<20} {_mb(sum(classify_run(r, min_age)['text_bytes'] for r in runs)):>9}  "
          "reports, extractions, critiques, provenance, metadata")

    print(f"\n[cleanup] {'freed' if any_apply else 'reclaimable'}: {_mb(freed_total)}")
    if not any_apply and freed_total:
        print("[cleanup] re-run with --caches / --images / --logs / --all to act")


if __name__ == "__main__":
    main()
