"""Offline Q&A resolution eval — does LORE find the RIGHT source page for a question?

This guards the deterministic retrieval/resolution layer (the layer the "BE260416" bug lived
in): given a question, it checks the page-candidate list `query_kb` would show — WITHOUT any
LLM, cookies, or Slack — against an expected page. It runs in seconds and is repeatable, so a
silent regression (a CLAUDE.md edit, a KG rebuild, a crawl-scope change, a ranking tweak) that
makes the wrong page surface becomes a loud failure instead of a wrong answer weeks later.

It does NOT test the LLM figure read or the exact numeric answer — those are non-deterministic
and vary per run. This tests only "did the right source get surfaced", which is deterministic.

Cases live in ``eval/qa_cases.jsonl``; each line is one of:
  {"q": "...", "expect_page": "la_page:<name>"}   # expected page must be in the candidates
  {"q": "...", "expect_no_id": true}              # no identifier in q may falsely resolve

Usage:  python -m lab_agent.cli.eval_qa            # runs the default cases file
        python -m lab_agent.cli.eval_qa path.jsonl
Exit code is non-zero if any case fails (so it can gate CI / a pre-test check).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..config import KNOWLEDGE_ROOT, PROJECT_ROOT
from . import ask

DEFAULT_CASES = PROJECT_ROOT / "eval" / "qa_cases.jsonl"
TOP_K = 6


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def run_case(case: dict) -> tuple[bool, str]:
    """Return (passed, detail)."""
    q = case["q"]
    if case.get("expect_no_id"):
        ids = ask.resolve_identifiers(q, 5)
        if ids:
            return False, f"expected NO identifier match, got {[s for _n, _o, s in ids]}"
        return True, "no false identifier match"
    expect = case["expect_page"]
    cands = [src for src, _tag in ask.candidate_pages(q, TOP_K)]
    if expect in cands:
        return True, f"found at rank {cands.index(expect) + 1}/{len(cands)}"
    return False, f"expected {expect} not in candidates {cands}"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = Path(args[0]) if args else DEFAULT_CASES
    if not path.exists():
        print(f"[eval_qa] cases file not found: {path}")
        sys.exit(2)

    # The eval resolves against the local crawled corpus; without it there is nothing to test.
    if not (KNOWLEDGE_ROOT / "labarchives").is_dir():
        print("[eval_qa] no local corpus (knowledge/labarchives) — run build_kb first. Skipping.")
        sys.exit(0)

    cases = load_cases(path)
    passed = 0
    for c in cases:
        ok, detail = run_case(c)
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {c['q'][:70]!r}\n      {detail}")
    print(f"\n[eval_qa] {passed}/{len(cases)} cases passed")
    sys.exit(0 if passed == len(cases) else 1)


if __name__ == "__main__":
    main()
