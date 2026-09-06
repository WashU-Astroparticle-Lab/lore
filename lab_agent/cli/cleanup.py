"""
Report and reclaim LORE's disk footprint.

    python -m lab_agent.cli.cleanup                 # report only (default)
    python -m lab_agent.cli.cleanup --apply         # prune the re-fetchable caches
    python -m lab_agent.cli.cleanup --apply --max-age-days 7 --max-mb 100

Dry-run by default: it prints what it *would* remove and removes nothing.

Only re-fetchable caches are pruned:
  knowledge/image_cache   LabArchives figures  (re-fetch: fetch_page_images)
  knowledge/slack_files   Slack attachments    (re-fetch: slack fetch-files)

Never touched, and reported for information only:
  outputs/                experiment runs — the record, and the largest consumer
  knowledge/labarchives/  the crawled corpus the knowledge graph is built from
  cache/                  small, and rebuilt on demand

Note: this tree usually lives inside OneDrive, which syncs every byte regardless
of .gitignore. Trimming here reduces sync traffic as well as disk.
"""
from __future__ import annotations

import sys
from pathlib import Path

from ..cache_prune import DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_MB, dir_size, plan_prune, prune
from ..config import KNOWLEDGE_ROOT, PROJECT_ROOT

PRUNABLE = [KNOWLEDGE_ROOT / "image_cache", KNOWLEDGE_ROOT / "slack_files"]
REPORT_ONLY = [PROJECT_ROOT / "outputs", KNOWLEDGE_ROOT / "labarchives", PROJECT_ROOT / "cache"]


def _mb(n: int) -> str:
    return f"{n / 1e6:8.1f} MB"


def main() -> None:
    args = sys.argv[1:]
    apply = "--apply" in args

    def _opt(name: str, default: int) -> int:
        if name in args:
            return int(args[args.index(name) + 1])
        return default

    max_age = _opt("--max-age-days", DEFAULT_MAX_AGE_DAYS)
    max_mb = _opt("--max-mb", DEFAULT_MAX_MB)

    print(f"[cleanup] {'APPLYING' if apply else 'dry run — nothing will be removed'}"
          f"  (keep < {max_age}d, cap {max_mb} MB per cache)\n")

    total_freed = 0
    print("Re-fetchable caches:")
    for root in PRUNABLE:
        if not root.exists():
            print(f"  {root.name:<16} (absent)")
            continue
        before = dir_size(root)
        doomed = plan_prune(root, max_age, max_mb)
        if apply:
            removed, freed = prune(root, max_age, max_mb, apply=True)
        else:
            removed, freed = len(doomed), sum(d[1] for d in doomed)
        total_freed += freed
        verb = "removed" if apply else "would remove"
        print(f"  {root.name:<16} {_mb(before)}  ->  {verb} {removed} entr(ies), {_mb(freed)}")
        for path, size, reason in doomed[:5]:
            print(f"      {path.name}  ({_mb(size).strip()}, {reason})")
        if len(doomed) > 5:
            print(f"      … and {len(doomed) - 5} more")

    print("\nNot pruned (the record, not a cache):")
    for root in REPORT_ONLY:
        size = dir_size(root) if root.exists() else 0
        note = ""
        if root.name == "outputs" and size > 500e6:
            note = "  <- large; delete individual experiment dirs by hand if needed"
        print(f"  {root.name:<16} {_mb(size)}{note}")

    print(f"\n[cleanup] {'freed' if apply else 'reclaimable'}: {_mb(total_freed).strip()}")
    if not apply and total_freed:
        print("[cleanup] re-run with --apply to actually remove them")


if __name__ == "__main__":
    main()
