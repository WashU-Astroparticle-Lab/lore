"""PreToolUse hook for LORE's Slack sessions: fridge logs are read only through run_dr.py.

The listener passes it to every session it starts (``claude --settings``). It reads the
hook's JSON on stdin and refuses any tool call that names a LogLCR file or the DR data
folder. A rule in CLAUDE.md was not enough: on 2026-11-06 a session copied a stray script,
merged the T4 column of every channel file and invented an "in calibration" range, and a
lab member spent days on a CMN223 file that read 5e5 mK. run_dr.py reads DR_DATA_PATH
itself, so nothing LORE is asked to do needs the folder in a command.

Standard library only, so any interpreter can run it. Exit 2 blocks the call and shows
the message to the session; anything it cannot read is let through.
"""
from __future__ import annotations

import json
import os
import sys

MESSAGE = (
    "Blocked: fridge logs are read only through run_dr.py. Use "
    '`python run_dr.py "<start>" "<end>" --csv "<label or channel>"` for one thermometer\'s '
    'readings, or `python run_dr.py "<start>" "<end>"` for the conditions summary. It reads '
    "each channel from its own file, names the thermometer and says what it left out. "
    "Reading LogLCR files yourself merges the channels (see CLAUDE.md, 'Fridge logs')."
)


def _variants(path: str) -> list[str]:
    p = path.strip().strip('"').replace("\\", "/").rstrip("/").lower()
    if not p:
        return []
    out = [p]
    if len(p) > 2 and p[1] == ":":
        out.append(f"/{p[0]}{p[2:]}")            # Git Bash: /c/LC Data/Data
    return out


def blocked(tool_input: object, data_path: str = "") -> bool:
    text = json.dumps(tool_input).replace("\\\\", "/").lower()
    if "loglcr" in text:
        return True
    return any(v in text for v in _variants(data_path))


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 — a hook that cannot read its input must not block
        return 0
    if blocked(event.get("tool_input", {}), os.environ.get("DR_DATA_PATH", "")):
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
