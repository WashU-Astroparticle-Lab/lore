"""Tests for the local Q&A search (lab_agent.cli.ask): relevance, provenance, scope.

Run: python -m pytest tests/test_ask_search.py   (or: python tests/test_ask_search.py)
"""
import tempfile
from pathlib import Path

import lab_agent.cli.ask as ask


def _mk(root: str, exp: str, files: dict[str, str]) -> None:
    d = Path(root) / exp
    d.mkdir(parents=True)
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")


def test_search_finds_and_attributes_provenance():
    with tempfile.TemporaryDirectory() as tmp:
        ask.OUTPUT_ROOT = Path(tmp)
        ask.KNOWLEDGE_ROOT = Path(tmp) / "no_kb"   # isolate: no knowledge bundle
        _mk(tmp, "power_cal_x", {"extracted_github.md": "DAC_CURRENT tested at 40500 uA saturation"})
        _mk(tmp, "cooldown_y", {"extracted_labarchives.md": "total attenuation 90 dB cold chain"})

        hits = ask.search("attenuation cold chain", k=5)
        assert hits and hits[0][1] == "cooldown_y", hits

        hits2 = ask.search("DAC_CURRENT saturation", k=5)
        assert hits2 and hits2[0][1] == "power_cal_x", hits2


def test_excludes_raw_dumps():
    # notebooks.md / data_summaries.md are noisy raw dumps and must not be searched.
    with tempfile.TemporaryDirectory() as tmp:
        ask.OUTPUT_ROOT = Path(tmp)
        ask.KNOWLEDGE_ROOT = Path(tmp) / "no_kb"   # isolate: no knowledge bundle
        _mk(tmp, "exp_z", {
            "notebooks.md": "attenuation attenuation attenuation",
            "data_summaries.md": "attenuation",
        })
        assert ask.search("attenuation", k=5) == []


def test_experiment_id_match_is_boosted():
    with tempfile.TemporaryDirectory() as tmp:
        ask.OUTPUT_ROOT = Path(tmp)
        ask.KNOWLEDGE_ROOT = Path(tmp) / "no_kb"   # isolate: no knowledge bundle
        _mk(tmp, "20260227_power_calibration", {"connections.md": "the calibration surface"})
        _mk(tmp, "other_run", {"connections.md": "the calibration surface"})
        hits = ask.search("calibration", k=5)
        assert hits[0][1] == "20260227_power_calibration", hits


def test_trailing_period_term_still_matches():
    # A query token like 'calibration.' must match 'calibration' in the text.
    with tempfile.TemporaryDirectory() as tmp:
        ask.OUTPUT_ROOT = Path(tmp)
        ask.KNOWLEDGE_ROOT = Path(tmp) / "no_kb"   # isolate: no knowledge bundle
        _mk(tmp, "exp", {"connections.md": "the calibration surface"})
        assert ask.search("tell me about the calibration.", 5), "trailing-dot term missed match"


def test_identifier_tokens_pick_ids_not_common_words():
    toks = ask._identifier_tokens(
        "mean Qc on the 9 devices with quantum capacitance in BE260416 run 20260702")
    assert "be260416" in toks          # mixed letters+digits → identifier
    assert "20260702" in toks          # long digit run → identifier
    assert "quantum" not in toks and "devices" not in toks   # plain words excluded


def test_resolve_identifiers_maps_chip_id_to_its_page():
    # The regression: a chip ID (only present inside a hyperlink filename) must map to its
    # page even though a distractor page is stuffed with the question's common concept words.
    with tempfile.TemporaryDirectory() as tmp:
        ask.OUTPUT_ROOT = Path(tmp) / "no_out"
        kb = Path(tmp) / "kb"
        ask.KNOWLEDGE_ROOT = kb
        la = kb / "labarchives"
        la.mkdir(parents=True)
        (la / "20260702_JPL_QPDs.md").write_text(
            "QC traces and quantum capacitance discussion. See "
            "https://github.com/x/BE260416-NG-D1-CPB_qct_20260709.ipynb for the data.",
            encoding="utf-8")
        (la / "20260402_Meeting_Notes.md").write_text(
            "quantum capacitance devices mean response meaningful measured Qc " * 20,
            encoding="utf-8")
        q = ("mean Qc on the 9 devices with meaningful response to quantum "
             "capacitance in BE260416?")
        # TF-IDF over the whole question is drawn to the common-word-stuffed distractor…
        kw_top = [src for _s, src, _f, _sn in ask.search(q, 5)][:1]
        assert kw_top == ["la_page:20260402_Meeting_Notes"], kw_top
        # …but the identifier resolver keys on the rare ID and nails the right page.
        id_hits = ask.resolve_identifiers(q, 5)
        assert id_hits and id_hits[0][2] == "la_page:20260702_JPL_QPDs", id_hits
        assert all("Meeting" not in src for _n, _o, src in id_hits)


def test_stopword_only_query_returns_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        ask.OUTPUT_ROOT = Path(tmp)
        ask.KNOWLEDGE_ROOT = Path(tmp) / "no_kb"   # isolate: no knowledge bundle
        _mk(tmp, "exp", {"connections.md": "content"})
        assert ask.search("what is the of and", 5) == []


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
