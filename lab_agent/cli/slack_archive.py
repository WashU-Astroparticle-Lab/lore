"""Archive the lab's Slack figures to LabArchives, one page per week (lab_agent.slack.archive).

Usage:
  python -m lab_agent.cli.slack_archive                    # the daily run: capture the last
                                                           # 21 days, post settled discussions,
                                                           # add updates for late replies
  python -m lab_agent.cli.slack_archive --capture-only [--days 90]
                                                           # save messages and figures locally
  python -m lab_agent.cli.slack_archive --backfill [--days 90] [--limit N]
                                                           # oldest first, N discussions per run
  python -m lab_agent.cli.slack_archive --preview DIR [--days N] [--limit N]
                                                           # summaries into DIR/<week>.html;
                                                           # nothing posted, no state saved

Channels come from lab_config.md's "Slack archive channels" row. The listener runs the daily
mode at the knowledge-graph refresh hour, just before the refresh.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ..config import load_env


def _value(args: list[str], flag: str, default=None, cast=str):
    if flag not in args:
        return default
    i = args.index(flag)
    if i + 1 >= len(args):
        print(f"{flag} needs a value")
        sys.exit(2)
    return cast(args[i + 1])


def main() -> None:
    load_env()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    from ..slack import archive

    args = sys.argv[1:]
    days = _value(args, "--days", None, int)
    limit = _value(args, "--limit", None, int)
    preview = _value(args, "--preview", None, Path)

    if "--capture-only" in args:
        now = time.time()
        state = archive.load_state()
        counts = archive.capture(archive.SlackSource(), archive.channels_configured(),
                                 now - (days or 90) * 86400, now, state=state)
        archive.save_state(state)
        print("[slack-archive] captured " + ", ".join(f"#{c} {n}" for c, n in counts.items()))
        return

    if "--backfill" in args:
        window = days or 90
        code, log = archive.run(capture_days=window, window_days=window, limit=limit,
                                preview_dir=preview)
    else:
        window = days or archive.FOLLOWUP_DAYS
        code, log = archive.run(capture_days=window, window_days=window, limit=limit,
                                preview_dir=preview)
    for line in log:
        print(f"[slack-archive] {line}", flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
