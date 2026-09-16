"""Tests for WS4 link verification (lab_agent.cli.verify_links) — offline parts.

URL extraction, LabArchives auth-walled short-circuit, GitHub→API URL construction,
and report formatting. Live network checks are exercised by a manual smoke run.

Run: python -m pytest tests/test_verify_links.py  (or: python tests/test_verify_links.py)
"""
import tempfile
from pathlib import Path

from lab_agent.cli import verify_links as vl


def test_extract_urls_dedup_and_strip():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "a.md").write_text(
            "see https://github.com/o/r/tree/main (and https://example.com/x).\n"
            "dup https://github.com/o/r/tree/main again\n", encoding="utf-8")
        (d / "b.md").write_text("ref https://mynotebook.labarchives.com/page/1\n", encoding="utf-8")
        (d / "c.md").write_text("code `https://github.com/o/r/tree/main` and **bold**\n"
                                "and https://github.com/o/r/blob/main/x.ipynb**\n", encoding="utf-8")
        urls = vl.extract_urls(d)
        assert "https://github.com/o/r/tree/main" in urls
        assert "https://example.com/x" in urls          # trailing ')' stripped
        assert "https://mynotebook.labarchives.com/page/1" in urls
        assert urls.count("https://github.com/o/r/tree/main") == 1   # deduped
        # trailing markdown chars (backtick, **) must be stripped, not kept on the URL
        assert "https://github.com/o/r/blob/main/x.ipynb" in urls
        assert not any(u.endswith(("`", "*")) for u in urls)


def test_labarchives_not_checked_no_network():
    # LabArchives is auth-walled; check_url must short-circuit to not_checked
    # (this call must make NO network request).
    status, final = vl.check_url("https://mynotebook.labarchives.com/some/page", token=None)
    assert status == "not_checked"


def test_github_regex_builds_contents_api_path():
    m = vl._GITHUB_TREE_BLOB_RE.match(
        "https://github.com/example-lab/experiments/tree/ac7ee2a1/DAQ/exp")
    assert m.group("owner") == "example-lab"
    assert m.group("repo") == "experiments"
    assert m.group("ref") == "ac7ee2a1"
    assert m.group("path") == "DAQ/exp"


def test_write_report_flags_dead_links():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        rows = [
            ("https://github.com/o/r", "ok", "https://github.com/o/r"),
            ("https://dead.example/x", "not_found", "https://dead.example/x"),
            ("https://mynotebook.labarchives.com/p", "not_checked", "https://mynotebook.labarchives.com/p"),
        ]
        vl.write_report(d, rows)
        txt = (d / "link_check.md").read_text(encoding="utf-8")
        assert "type: Link Check" in txt
        assert "| https://github.com/o/r | ok |" in txt
        assert "1 dead" in txt
        assert "https://dead.example/x" in txt.split("Do not cite")[1]


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
