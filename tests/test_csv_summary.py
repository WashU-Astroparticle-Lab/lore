"""Tests for WS6 CSV summarization (lab_agent.collect.summarize._summarize_large_csv).

Covers numeric per-column stats, non-numeric value sampling, timestamp-column
detection, and the first/last sample rows + unverifiability note.

Run: python -m pytest tests/test_csv_summary.py  (or: python tests/test_csv_summary.py)
"""
from lab_agent.collect.summarize import _summarize_large_csv, _looks_like_timestamp


def _rows(header, n, make):
    return [header] + [make(i) for i in range(n)]


def test_numeric_column_stats():
    rows = _rows(["freq", "power"], 30, lambda i: [str(2 + i), str(-i)])
    out = _summarize_large_csv("data.csv", rows)
    assert "freq" in out and "mean=" in out and "median=" in out
    assert "min=" in out and "max=" in out
    assert "30 rows" in out


def test_non_numeric_values_sampled():
    rows = _rows(["channel"], 30, lambda i: [f"ch{i % 3}"])   # 3 distinct values
    out = _summarize_large_csv("d.csv", rows)
    assert "channel" in out and "(text)" in out
    assert "ch0" in out and "ch1" in out and "ch2" in out


def test_timestamp_column_detected():
    rows = _rows(["t", "v"], 25, lambda i: [f"2026-02-{(i % 27) + 1:02d} 10:00:00", str(i)])
    out = _summarize_large_csv("ts.csv", rows)
    assert "(timestamp)" in out
    assert "first=" in out and "last=" in out


def test_sample_rows_and_note():
    rows = _rows(["a", "b"], 40, lambda i: [str(i), str(i * 2)])
    out = _summarize_large_csv("s.csv", rows)
    assert "First rows:" in out and "Last rows:" in out
    assert "| a | b |" in out                       # markdown table header
    assert "Full data not included" in out and "40 rows" in out


def test_looks_like_timestamp_rejects_plain_numbers():
    assert _looks_like_timestamp("2026-02-18 10:00:00") is True
    assert _looks_like_timestamp("2026-03-04 18:11:22.432") is True   # fractional seconds
    assert _looks_like_timestamp("40500") is False
    assert _looks_like_timestamp("") is False


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
