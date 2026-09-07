"""Read Phase D's verdict, so the upload can refuse to run without it.

The critic writes `critique.md` ending in a `## Summary` line carrying exactly
one outcome (`passed`, `gaps_found: …`, `expert_needed: …`, `human_needed: …`).
`.claude/skills/experiment-report/SKILL.md` says `gaps_found` triggers one
revision pass *before* the report is uploaded.

On the 2026-09-06 warm-amp run that did not happen. The critic wrote
`gaps_found: 1` at 23:16 and the report went to LabArchives at 23:17 with no
revision and no second critique — the orchestrator had the rule in front of it
and walked past it. The Aug 26 report reached the notebook having never been
critiqued at all. A rule in a prompt is a suggestion; a guard in code is a
constraint, so the gate lives here now and `cli/upload.py` calls it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

OUTCOMES = ("passed", "gaps_found", "expert_needed", "human_needed")

# The outcome may be wrapped in backticks or bold markers — the real file had
# it as `gaps_found: 1`. Strip decoration, then match the bare token.
_DECORATION = "`*_ \t"
_LINE = re.compile(rf"^({'|'.join(OUTCOMES)})\b\s*:?\s*(.*)$")

# Critiques written before the four-outcome convention end with `FAIL (1)` or
# `FAIL - Items 1, 3 and 7 failed.` instead. Runs on disk still look like that,
# and calling them "unreadable" is vague about a critique that plainly says the
# report failed. This recognises the failure only: a legacy `PASS` is NOT a
# green light, because per-item PASS lines are indistinguishable from a summary
# verdict and the last one would wave a bad report through. `passed` stays the
# one explicit way to authorise an upload.
_LEGACY_FAIL = re.compile(r"^FAIL([^A-Za-z0-9].*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """What Phase D concluded. `outcome` is one of OUTCOMES, or:

    ``missing``    — no critique.md; the report was never critiqued.
    ``unreadable`` — critique.md exists but states no recognised outcome.
    """

    outcome: str
    detail: str = ""
    path: Path | None = None

    @property
    def clean(self) -> bool:
        return self.outcome == "passed"


def read_verdict(out_dir: str | Path) -> Verdict:
    """Parse `<out_dir>/critique.md`. Never raises — an unreadable critique is
    itself a verdict, and one that blocks."""
    path = Path(out_dir) / "critique.md"
    if not path.is_file():
        return Verdict("missing", path=path)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Verdict("unreadable", f"could not read critique.md: {exc}", path)

    # Prefer the last `## Summary` section; fall back to the whole file. The
    # body quotes checklist wording, so the LAST standalone outcome line wins.
    lower = text.lower()
    cut = lower.rfind("## summary")
    body = text[cut:] if cut != -1 else text

    found: Verdict | None = None
    legacy: Verdict | None = None
    for line in body.splitlines():
        stripped = line.strip().strip(_DECORATION)
        m = _LINE.match(stripped)
        if m:
            found = Verdict(m.group(1), m.group(2).strip().strip(_DECORATION), path)
            continue
        if _LEGACY_FAIL.match(stripped):
            legacy = Verdict("gaps_found", f"{stripped} (legacy critique format)", path)
    if found is not None:
        return found
    if legacy is not None:
        return legacy
    return Verdict("unreadable", "no `## Summary` outcome line found", path)


def blocks_upload(verdict: Verdict) -> bool:
    """Anything short of a clean pass blocks. `passed` is the only green light."""
    return not verdict.clean


def explain(verdict: Verdict) -> str:
    """The message a blocked upload prints — say what to do, not just no."""
    v = verdict
    where = v.path or "critique.md"
    if v.outcome == "gaps_found":
        return (
            f"[upload] BLOCKED: the critic failed this report — gaps_found: {v.detail}\n"
            f"  {where}\n"
            "  Phase D found fixable problems and the revision pass has not run.\n"
            "  Do this instead of uploading:\n"
            "    1. spawn report-writer with '<out_dir>. Revision mode — fix only "
            "the FAIL items in critique.md.'\n"
            "    2. re-spawn the critic\n"
            "    3. upload once it reports `passed`\n"
            "  Override only if you have read the critique and disagree: --force"
        )
    if v.outcome in ("expert_needed", "human_needed"):
        return (
            f"[upload] BLOCKED: the critic asked for a human — {v.outcome}: {v.detail}\n"
            f"  {where}\n"
            "  Do not upload. Put the critic's question to the user and stop.\n"
            "  Override only with explicit approval: --force"
        )
    if v.outcome == "missing":
        return (
            f"[upload] BLOCKED: no critique.md in this run — the report has never "
            "been critiqued.\n"
            f"  expected at {where}\n"
            "  Spawn the critic first (Phase D). An uncritiqued report reached the "
            "lab notebook once already.\n"
            "  Override for a run that legitimately has no critic (e.g. a hand-written "
            "report): --force"
        )
    return (
        f"[upload] BLOCKED: critique.md states no recognised outcome — {v.detail}\n"
        f"  {where}\n"
        f"  The `## Summary` line must be one of: {', '.join(OUTCOMES)}.\n"
        "  Re-spawn the critic. Override: --force"
    )
