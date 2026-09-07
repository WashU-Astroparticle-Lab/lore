"""
Durable per-user report preferences.

    python -m lab_agent.cli.preferences show [--user U123]
    python -m lab_agent.cli.preferences set  --user U123 --note "no Key Parameters table"

Why: style preferences were being re-established every time. "Shorter", "drop
the Key Parameters table", "power in dBm only" were all agreed on one run and
gone by the next, so the same corrections were paid for twice. A preference the
user has stated once should survive into the next report.

Preferences are a per-user markdown file under ``knowledge/preferences/``. They
shape *presentation only* — never which numbers are reported, and never whether
a claim is hedged.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

from ..config import KNOWLEDGE_ROOT, load_env

PREFS_DIR = Path(KNOWLEDGE_ROOT) / "preferences"

_HEADER = """---
type: User Preferences
resource: {user}
---

# Report preferences — {user}

Presentation only. These never change which numbers are reported, whether a
claim is hedged, or what the critic checks.

"""


def prefs_path(user: str) -> Path:
    safe = "".join(c for c in user if c.isalnum() or c in "-_") or "default"
    return PREFS_DIR / f"{safe}.md"


def read_prefs(user: str) -> str:
    """Return the user's preference notes ('' when none recorded)."""
    path = prefs_path(user)
    if not path.exists():
        return ""
    lines = [
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.startswith("- ")
    ]
    return "\n".join(lines)


def add_pref(user: str, note: str) -> Path:
    """Append a preference, ignoring an exact duplicate."""
    note = note.strip().rstrip(".")
    if not note:
        raise ValueError("empty preference")
    path = prefs_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_HEADER.format(user=user), encoding="utf-8")
    existing = path.read_text(encoding="utf-8")
    if f"- {note}" in existing:
        return path
    stamp = _dt.date.today().isoformat()
    path.write_text(existing.rstrip("\n") + f"\n- {note}  _(stated {stamp})_\n", encoding="utf-8")
    return path


def main() -> None:
    load_env()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd, rest = args[0], args[1:]

    opts: dict = {}
    i = 0
    while i < len(rest):
        key, _, inline = rest[i].partition("=")
        field = key.lstrip("-").replace("-", "_")
        if inline:
            opts[field] = inline
            i += 1
        elif i + 1 < len(rest):
            opts[field] = rest[i + 1]
            i += 2
        else:
            print(f"[prefs] {key} needs a value")
            sys.exit(1)

    user = opts.get("user", "default")
    if cmd == "show":
        notes = read_prefs(user)
        print(notes if notes else f"[prefs] none recorded for {user}")
    elif cmd == "set":
        if not opts.get("note"):
            print("[prefs] set needs --note")
            sys.exit(1)
        path = add_prefs_safe(user, opts["note"])
        print(f"[prefs] recorded for {user} -> {path}")
    else:
        print(f"[prefs] unknown command {cmd!r} (show|set)")
        sys.exit(1)


def add_prefs_safe(user: str, note: str) -> Path:
    try:
        return add_pref(user, note)
    except ValueError as exc:
        print(f"[prefs] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
