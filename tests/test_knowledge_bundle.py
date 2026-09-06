"""Tests for Stage 4 knowledge bundle (record_knowledge) + ask integration + signing.

Run: python -m pytest tests/test_knowledge_bundle.py
     (or: python tests/test_knowledge_bundle.py)
"""
import json
import tempfile
from pathlib import Path

import lab_agent.cli.record_knowledge as rk
import lab_agent.cli.ask as ask

_REPORT = """# [UNSIGNED] exp_x

**Experiment:** exp_x

## Executive Summary
We characterised the power calibration; saturation amplitude ~0.599 at 40,500 uA.

## Objective
Measure output power versus DAC amplitude across the band.

## Key Parameters
| Parameter | Value | Source |
| --- | --- | --- |
| DAC_CURRENT | 40500 uA | dependency source |

## Results
Details here.
"""


def _setup_out(tmp: str) -> Path:
    d = Path(tmp) / "outputs" / "exp_x"
    d.mkdir(parents=True)
    (d / "metadata.json").write_text(json.dumps({
        "experiment_id": "exp_x",
        "la_pages": ["20260303 Amplitude & DAC Sweep"],
        "github_url": "https://github.com/o/r/tree/main",
        "github_commit_sha": "abc1234def5678",
        "github_commit_date": "2026-02-27T00:00:00Z",
    }), encoding="utf-8")
    (d / "[UNSIGNED] exp_x.md").write_text(_REPORT, encoding="utf-8")
    return d


def test_record_writes_concept_log_and_index():
    with tempfile.TemporaryDirectory() as tmp:
        rk.KNOWLEDGE_ROOT = Path(tmp) / "knowledge"
        path = rk.record(_setup_out(tmp))
        text = path.read_text(encoding="utf-8")
        assert "type: Experiment" in text and "status: unsigned" in text
        assert "DAC_CURRENT" in text                      # key params captured
        assert "Measure output power" in text             # objective captured
        assert "abc1234" in text                          # commit provenance
        assert (Path(tmp) / "knowledge" / "log.md").exists()
        idx = (Path(tmp) / "knowledge" / "index.md").read_text(encoding="utf-8")
        assert "exp_x.md" in idx and "unsigned" in idx


def test_sign_promotes_to_signed():
    with tempfile.TemporaryDirectory() as tmp:
        rk.KNOWLEDGE_ROOT = Path(tmp) / "knowledge"
        path = rk.record(_setup_out(tmp), sign=True)
        assert "status: signed" in path.read_text(encoding="utf-8")


def test_ask_searches_knowledge_bundle():
    with tempfile.TemporaryDirectory() as tmp:
        rk.KNOWLEDGE_ROOT = Path(tmp) / "knowledge"
        ask.KNOWLEDGE_ROOT = Path(tmp) / "knowledge"
        ask.OUTPUT_ROOT = Path(tmp) / "outputs"
        rk.record(_setup_out(tmp))
        hits = ask.search("saturation amplitude power calibration", k=5)
        assert hits, "no hits"
        assert hits[0][1].startswith("knowledge:"), hits   # curated concept ranks


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
