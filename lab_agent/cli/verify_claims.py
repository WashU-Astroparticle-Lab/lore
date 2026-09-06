"""
Check every number in a draft against its sources.

    python -m lab_agent.cli.verify_claims --draft msg.txt --source outputs/<id>
    python -m lab_agent.cli.verify_claims --draft msg.txt --source a.md --source b.md

Why: the report pipeline gates its own Slack summary (critic item 10), but a
message *drafted* for a channel had no check at all — and drafts are prose about
experiment results written from memory. One real draft merged two different
ranges ("+0.01 to +0.35 dBm at both tones" when the two tones were +0.01–0.15 and
+0.13–0.35) and dropped an open question the report had flagged.

This is a deterministic tripwire, not a proof. It answers one narrow question —
"does this number appear anywhere in the sources?" — which catches invented,
merged and mistyped values. It cannot catch a real number attached to the wrong
label, or a claim with no number in it. Those still need reading.

Exit 0 when every number is accounted for, 1 when any is not.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Numbers as they appear in physics prose: optional sign, digits, optional
# decimal, optional exponent. Deliberately ignores the unit — "4 dBm" and "4 dB"
# are the same number, and unit drift is a reading problem, not a matching one.
_NUM = re.compile(r"[-+−]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

# Numbers too generic to be evidence of anything.
_TRIVIAL = {"0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "100", "1000"}


def numbers_in(text: str) -> list[str]:
    """Distinct numeric tokens, normalised (unicode minus -> ascii, sign dropped)."""
    out, seen = [], set()
    for raw in _NUM.findall(text):
        n = raw.replace("−", "-").lstrip("+-").rstrip(".")
        if not n or n in _TRIVIAL or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


# A run directory contributes only its NARRATIVE files. Pointing this at the whole
# directory was the first version's mistake: notebooks.md and the extractions hold
# tens of thousands of numbers, so almost any value "appears somewhere" and the
# check passes everything. Caught by a positive control — invented values of 62,
# 71 and 7.5 all sailed through. The meaningful question is whether a number is in
# the REPORT the draft claims to summarise.
_NARRATIVE_GLOBS = ("[[]UNSIGNED[]] *.md", "provenance.md", "slack_summary.md")


def _run_dir_sources(path: Path) -> list[Path]:
    out: list[Path] = []
    for pattern in _NARRATIVE_GLOBS:
        out += sorted(path.glob(pattern))
    return out


def _source_text(paths: list[str]) -> str:
    """Concatenate every source. A run directory contributes its narrative files only."""
    parts, used = [], []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files = _run_dir_sources(path)
            if not files:
                print(f"[verify] {path.name}/ has no report or provenance to check against — "
                      "name the file explicitly with --source")
            for f in files:
                try:
                    parts.append(f.read_text(encoding="utf-8", errors="replace"))
                    used.append(f.name)
                except OSError:
                    continue
        elif path.is_file():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
            used.append(path.name)
        else:
            print(f"[verify] source not found: {p}")
    if used:
        print(f"[verify] checking against: {', '.join(used)}")
    return "\n".join(parts)


def unsupported(draft: str, sources: str) -> list[str]:
    """Numbers in the draft that appear nowhere in the sources."""
    hay = sources.replace("−", "-")
    return [n for n in numbers_in(draft) if n not in hay]


def main() -> None:
    # Physics prose is full of unicode minus (U+2212) and micro signs; the default
    # Windows console codec (cp1252) cannot encode them and the tool dies while
    # printing its own findings.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = sys.argv[1:]
    draft_path, sources = None, []
    i = 0
    while i < len(args):
        if args[i] == "--draft" and i + 1 < len(args):
            draft_path = args[i + 1]
            i += 2
        elif args[i] == "--source" and i + 1 < len(args):
            sources.append(args[i + 1])
            i += 2
        else:
            print(__doc__)
            sys.exit(1)
    if not draft_path or not sources:
        print(__doc__)
        sys.exit(1)

    draft = Path(draft_path)
    if not draft.exists():
        print(f"[verify] draft not found: {draft}")
        sys.exit(1)

    text = draft.read_text(encoding="utf-8")
    missing = unsupported(text, _source_text(sources))
    total = len(numbers_in(text))

    if not missing:
        print(f"[verify] all {total} number(s) in {draft.name} appear in the sources.")
        print("[verify] NOTE: this checks presence only. A number attached to the wrong")
        print("         label, or a claim with no number, still needs reading.")
        return

    print(f"[verify] {len(missing)} of {total} number(s) in {draft.name} appear in NO source:")
    for n in missing:
        for line in text.splitlines():
            if n in line:
                print(f"  {n}   <- {line.strip()[:110]}")
                break
    print("\n[verify] Either the number is wrong, or it was derived/merged rather than")
    print("         quoted. Say which in the draft, or take it out.")
    sys.exit(1)


if __name__ == "__main__":
    main()
