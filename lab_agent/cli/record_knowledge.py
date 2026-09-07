"""Record an experiment into the cross-run knowledge bundle (OKF Phase 3).

After a report is uploaded, distil one **Experiment concept** from the run into
``knowledge/experiments/<experiment_id>.md`` (objective, key parameters, outcome,
and provenance), append a dated ``knowledge/log.md`` entry, and refresh
``knowledge/index.md``. This is the lab's greppable, git-diffable long-term memory —
the same bundle the Q&A fast-path (`lab_agent.cli.ask`) searches, and the storage
spine a later LightRAG layer (Stage 5) would index.

The bundle is a *finding* aid built from already-produced reports; it is never a
source of new report numbers. Concepts are marked ``status: unsigned`` until a human
signs the report (``--sign`` promotes them to a high-trust ``signed`` exemplar).

Usage:
  python -m lab_agent.cli.record_knowledge outputs/<experiment_id> [--sign]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ..config import KNOWLEDGE_ROOT
from ..collect.okf import frontmatter


def _section(report: str, *keywords: str) -> str:
    """Return the body under the first heading containing any keyword, up to the next heading."""
    lines = report.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^#{1,6}\s+(.+)$", line)
        if m and any(k in m.group(1).lower() for k in keywords):
            body: list[str] = []
            for nxt in lines[i + 1:]:
                if re.match(r"^#{1,6}\s+", nxt):
                    break
                body.append(nxt)
            return "\n".join(body).strip()
    return ""


def _find_report(out_dir: Path) -> Path | None:
    reports = sorted(out_dir.glob("[[]UNSIGNED[]] *.md"))
    return reports[-1] if reports else None


def record(out_dir: Path, sign: bool = False) -> Path:
    meta = {}
    mp = out_dir / "metadata.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    exp_id = meta.get("experiment_id") or out_dir.name

    report_path = _find_report(out_dir)
    report = report_path.read_text(encoding="utf-8", errors="replace") if report_path else ""
    objective = _section(report, "objective") or _section(report, "executive summary")
    key_params = _section(report, "key parameters")
    summary = _section(report, "executive summary")
    outcome = summary.split("\n\n")[0].strip() if summary else "(no executive summary)"

    la_pages = meta.get("la_pages") or []
    tags = [exp_id] + [re.sub(r"[^0-9A-Za-z]+", "_", p)[:40] for p in la_pages]

    body_parts = [f"# {exp_id}", ""]
    if meta.get("github_url"):
        body_parts.append(f"**GitHub:** {meta['github_url']}")
    if la_pages:
        body_parts.append(f"**LabArchives:** {' · '.join(la_pages)}")
    if meta.get("github_commit_sha"):
        body_parts.append(f"**Commit:** `{meta['github_commit_sha'][:7]}` ({meta.get('github_commit_date', '')})")
    body_parts += ["", "## Objective", objective or "(none extracted)", ""]
    if key_params:
        body_parts += ["## Key Parameters", key_params, ""]
    body_parts += ["## Outcome", outcome, ""]

    fm = frontmatter(
        "Experiment",
        resource=meta.get("github_url", "") or exp_id,
        source_ref=meta.get("github_commit_sha"),
        timestamp=meta.get("github_commit_date"),
        tags=tags,
    )
    # status line inside the frontmatter (before the closing ---)
    fm = fm.replace("---\n\n", f"status: {'signed' if sign else 'unsigned'}\n---\n\n", 1)

    exp_dir = KNOWLEDGE_ROOT / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    concept_path = exp_dir / f"{exp_id}.md"
    concept_path.write_text(fm + "\n".join(body_parts), encoding="utf-8")

    _append_log(exp_id, signed=sign)
    _refresh_index()
    return concept_path


def _append_log(exp_id: str, signed: bool) -> None:
    log = KNOWLEDGE_ROOT / "log.md"
    header = "" if log.exists() else "# Knowledge Log\n\n"
    action = "signed" if signed else "recorded"
    with log.open("a", encoding="utf-8") as f:
        if header:
            f.write(header)
        f.write(f"- {action}: [[experiments/{exp_id}]]\n")


def _refresh_index() -> None:
    exp_dir = KNOWLEDGE_ROOT / "experiments"
    rows = []
    for f in sorted(exp_dir.glob("*.md")) if exp_dir.is_dir() else []:
        head = f.read_text(encoding="utf-8", errors="replace")[:400]
        status = re.search(r"^status:\s*(\w+)", head, re.M)
        rows.append(f"- [experiments/{f.name}](experiments/{f.name}) — {status.group(1) if status else 'unsigned'}")
    body = "# Knowledge Bundle Index\n\n## Experiments\n\n" + ("\n".join(rows) or "(none)") + "\n"
    KNOWLEDGE_ROOT.mkdir(parents=True, exist_ok=True)
    (KNOWLEDGE_ROOT / "index.md").write_text(frontmatter("Index") + body, encoding="utf-8")


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--sign"]
    sign = "--sign" in sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    out_dir = Path(args[0])
    if not out_dir.is_dir():
        print(f"Not a directory: {out_dir}")
        sys.exit(2)
    path = record(out_dir, sign=sign)
    print(f"[record_knowledge] wrote {path} (status={'signed' if sign else 'unsigned'})")


if __name__ == "__main__":
    main()
