"""Tests for the Tier-2 fetch helper's page lookup (lab_agent.cli.fetch_page_images).

Only the manifest lookup is unit-testable offline; the actual image download needs
network + a LabArchives session cookie and is exercised live.

Run: python -m pytest tests/test_fetch_page_images.py
"""
import json
import tempfile
from pathlib import Path

from lab_agent.cli.fetch_page_images import lookup_page


def _manifest(tmp: str) -> Path:
    p = Path(tmp) / "labarchives_images.json"
    p.write_text(json.dumps({"pages": {
        "SQUAT_Cooldown_Apr_14": {
            "page": "SQUAT Cooldown Apr 14", "nbid": "NB", "tree_id": "T_p2",
            "images": ["https://la/inline/a.png", "https://la/inline/b.png"],
        }
    }}), encoding="utf-8")
    return p


def test_lookup_by_exact_key():
    with tempfile.TemporaryDirectory() as t:
        r = lookup_page("SQUAT_Cooldown_Apr_14", _manifest(t))
        assert r and r["tree_id"] == "T_p2" and len(r["images"]) == 2


def test_lookup_by_title_or_safe_name():
    with tempfile.TemporaryDirectory() as t:
        mf = _manifest(t)
        assert lookup_page("SQUAT Cooldown Apr 14", mf) is not None          # title
        assert lookup_page("SQUAT Cooldown Apr 14!!!", mf) is not None        # safe-name of a title


def test_lookup_miss_returns_none():
    with tempfile.TemporaryDirectory() as t:
        assert lookup_page("no such page", _manifest(t)) is None


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
