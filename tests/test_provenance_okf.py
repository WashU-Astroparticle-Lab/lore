"""Tests for WS2 provenance + OKF Phase 1 frontmatter.

Covers the new CollectedArtifact provenance fields, the LabArchives entry-timestamp
parsing (guarded tag lookup), and the OKF frontmatter/index helpers. No network.

Run: python -m pytest tests/test_provenance_okf.py  (or: python tests/test_provenance_okf.py)
"""
import tempfile
from pathlib import Path

from lab_agent.models import CollectedArtifact
from lab_agent.collect import okf
from lab_agent.sources.labarchives.adapter import LabArchivesAdapter


def test_collected_artifact_provenance_fields_default_none():
    a = CollectedArtifact(path="p", kind="notes")
    assert a.source_ref is None and a.created_at is None and a.updated_at is None
    b = CollectedArtifact(path="p", kind="notes", source_ref="daq@abc1234",
                          created_at="2026-02-27T00:00:00Z")
    assert b.source_ref == "daq@abc1234" and b.created_at.startswith("2026")


def test_la_entry_timestamps_parsed_when_present():
    xml = """<tree><entry>
        <caption>Note</caption>
        <entry-data>hello world</entry-data>
        <eid>ENTRY-42</eid>
        <created-at>2026-02-18T14:03:00Z</created-at>
        <updated-at>2026-02-18T15:00:00Z</updated-at>
    </entry></tree>"""
    entries = LabArchivesAdapter._parse_entries_xml(xml)
    assert len(entries) == 1
    e = entries[0]
    assert e["content"] == "hello world"
    assert e["eid"] == "ENTRY-42"
    assert e["created"] == "2026-02-18T14:03:00Z"
    assert e["updated"] == "2026-02-18T15:00:00Z"


def test_la_entry_timestamps_none_when_absent():
    xml = "<tree><entry><caption>N</caption><entry-data>text</entry-data></entry></tree>"
    e = LabArchivesAdapter._parse_entries_xml(xml)[0]
    assert e["eid"] is None and e["created"] is None and e["updated"] is None


def test_okf_frontmatter_format():
    fm = okf.frontmatter("Notebooks", resource="https://github.com/o/r/tree/main",
                         source_ref="o/r@abc1234", timestamp="2026-02-27T00:00:00Z",
                         tags=["exp_x"])
    assert fm.startswith("---\ntype: Notebooks\n")
    assert "resource: https://github.com/o/r/tree/main" in fm
    assert "source_ref: o/r@abc1234" in fm
    assert "tags: [exp_x]" in fm
    assert fm.rstrip().endswith("---")


def test_okf_finalize_bundle_adds_frontmatter_and_index_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "notebooks.md").write_text("# notebook body\ncode", encoding="utf-8")
        (d / "labarchives.md").write_text("lab notes body", encoding="utf-8")

        okf.finalize_bundle(d, resource="https://github.com/o/r/tree/main",
                            source_ref="o/r@abc1234", timestamp="2026-02-27T00:00:00Z",
                            tags=["exp_x"])

        nb = (d / "notebooks.md").read_text(encoding="utf-8")
        assert nb.startswith("---\ntype: Notebooks\n")
        assert "source_ref: o/r@abc1234" in nb          # GitHub file carries provenance
        la = (d / "labarchives.md").read_text(encoding="utf-8")
        assert la.startswith("---\ntype: Lab Notes\n")
        assert "source_ref:" not in la.split("---", 2)[1]  # LA file has no GitHub ref

        idx = (d / "index.md").read_text(encoding="utf-8")
        assert "type: Index" in idx
        assert "notebooks.md" in idx and "Notebooks" in idx
        assert "labarchives.md" in idx and "Lab Notes" in idx

        # Idempotent: a second finalize must not double-prepend frontmatter.
        okf.finalize_bundle(d, resource="https://github.com/o/r/tree/main")
        nb2 = (d / "notebooks.md").read_text(encoding="utf-8")
        assert nb2.count("type: Notebooks") == 1


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
