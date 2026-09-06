"""Tests for incremental KG refresh (lab_agent.rag.incremental).

The planner decides which docs cost LLM tokens on refresh: only new + content-changed
ones re-index; formatting-only edits and unchanged docs are skipped; removed docs are
flagged for deletion.

Run: python -m pytest tests/test_incremental.py  (or: python tests/test_incremental.py)
"""
import tempfile
from pathlib import Path

from lab_agent.rag.incremental import (
    normalized_hash, plan_refresh, load_manifest, save_manifest,
)


def test_normalized_hash_ignores_formatting_but_not_content():
    assert normalized_hash("Hello,  World!") == normalized_hash("hello world")
    assert normalized_hash("f0 = 5 GHz") != normalized_hash("f0 = 6 GHz")   # real change


def test_plan_classifies_new_changed_unchanged_removed():
    manifest = {
        "a": normalized_hash("old A content"),
        "b": normalized_hash("B stays the same"),
        "gone": "deadbeef",
    }
    docs = {
        "a": "completely new A content",     # changed
        "b": "B stays the same",             # unchanged
        "c": "brand new C",                  # new
    }
    plan = plan_refresh(docs, manifest)
    assert plan["new"] == ["c"]
    assert plan["changed"] == ["a"]
    assert plan["unchanged"] == ["b"]
    assert plan["removed"] == ["gone"]
    assert set(plan["next_manifest"]) == {"a", "b", "c"}     # 'gone' dropped


def test_reindex_then_unchanged_costs_nothing():
    docs = {"a": "content A", "b": "content B"}
    first = plan_refresh(docs, {})
    assert set(first["new"]) == {"a", "b"}
    # After saving next_manifest, a second refresh of the same docs re-indexes nothing.
    second = plan_refresh(docs, first["next_manifest"])
    assert second["new"] == [] and second["changed"] == []
    assert set(second["unchanged"]) == {"a", "b"}


def test_formatting_only_edit_is_not_reindexed():
    m = plan_refresh({"a": "f0 = 5 GHz\nnote"}, {})["next_manifest"]
    plan = plan_refresh({"a": "F0 = 5   GHz\n\n  NOTE  "}, m)   # whitespace/case only
    assert plan["unchanged"] == ["a"] and plan["changed"] == []


def test_manifest_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "kb_manifest.json"
        save_manifest({"la_page:x": "hash1"}, p)
        assert load_manifest(p) == {"la_page:x": "hash1"}
        assert load_manifest(Path(tmp) / "missing.json") == {}   # tolerant of absence


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
