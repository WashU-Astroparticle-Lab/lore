"""Offline Q&A resolution regression net (see lab_agent/cli/eval_qa.py).

Runs the committed eval/qa_cases.jsonl against the local crawled corpus and asserts every
case resolves to the right page (or, for negatives, does not falsely resolve). This is the
guard for the class of bug where the wrong LabArchives page is surfaced for a question — the
"BE260416" failure. It SKIPS when there is no local corpus (e.g. CI), since there's nothing
to resolve against; run `build_kb` locally to exercise it.

Run: python tests/test_qa_resolution.py   (or via pytest)
"""
from lab_agent.cli import eval_qa
from lab_agent.config import KNOWLEDGE_ROOT

_HAS_CORPUS = (KNOWLEDGE_ROOT / "labarchives").is_dir()


def test_qa_cases_resolve():
    if not _HAS_CORPUS:
        print("SKIP (no local corpus)")
        return
    cases = eval_qa.load_cases(eval_qa.DEFAULT_CASES)
    if not cases:
        print("SKIP (no local eval cases)")
        return
    failures = []
    for c in cases:
        ok, detail = eval_qa.run_case(c)
        if not ok:
            failures.append(f"{c['q'][:60]!r}: {detail}")
    assert not failures, "QA resolution regressions:\n" + "\n".join(failures)


if __name__ == "__main__":
    import sys

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
