"""Tests for Stage 5a LabArchives crawler (lab_agent.collect.la_crawl).

Uses a fake adapter (real ElementTree nodes + the real _parse_entries_xml) so the tree
traversal, page-text extraction, corpus write, and ask() integration are all covered
without network. A live smoke run exercises the real adapter separately.

Run: python -m pytest tests/test_la_crawl.py  (or: python tests/test_la_crawl.py)
"""
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from lab_agent.collect import la_crawl
from lab_agent.sources.labarchives.adapter import LabArchivesAdapter
import lab_agent.cli.ask as ask


class FakeLA:
    """Minimal stand-in exposing the adapter methods the crawler uses."""

    def _list_notebooks(self):
        return [{"nbid": "NB1", "name": "Qubit & KID"}]

    def _get_tree_level(self, nbid, parent="0"):
        if parent == "0":
            xml = ("<r>"
                   "<level-node><tree-id>T_folder</tree-id>"
                   "<display-text>Folder</display-text><is-page>false</is-page></level-node>"
                   "<level-node><tree-id>T_ai</tree-id>"
                   "<display-text>AI Agent</display-text><is-page>false</is-page></level-node>"
                   "<level-node><tree-id>T_p1</tree-id>"
                   "<display-text>20260303 Amplitude Sweep</display-text><is-page>true</is-page></level-node>"
                   "</r>")
        elif parent == "T_folder":
            xml = ("<r><level-node><tree-id>T_p2</tree-id>"
                   "<display-text>SQUAT Cooldown Apr 14</display-text><is-page>true</is-page></level-node></r>")
        elif parent == "T_ai":
            xml = ("<r><level-node><tree-id>T_ai_p</tree-id>"
                   "<display-text>[UNSIGNED] some_report</display-text><is-page>true</is-page></level-node></r>")
        else:
            xml = "<r></r>"
        return ET.fromstring(xml).findall(".//level-node")

    def _get_entries_for_page(self, nbid, tree_id):
        body = f"content for {tree_id}"
        if tree_id == "T_p2":   # this page has an embedded figure
            body += ' &lt;img src="https://la/inline/plot.png"&gt;'
        return f"<tree><entry><caption>Note</caption><entry-data>{body}</entry-data></entry></tree>"

    _parse_entries_xml = staticmethod(LabArchivesAdapter._parse_entries_xml)


def setup_module(_module):
    la_crawl.page_index.record_pages = lambda *a, **k: None   # don't touch the real cache


def test_crawl_enumerates_all_pages_and_extracts_text():
    corpus = la_crawl.crawl(adapter=FakeLA(), exclude_folders={"AI Agent"})
    pages = {d["page"] for d in corpus}
    assert pages == {"20260303 Amplitude Sweep", "SQUAT Cooldown Apr 14"}
    p1 = next(d for d in corpus if d["tree_id"] == "T_p1")
    assert p1["text"] == "content for T_p1" and p1["notebook"] == "Qubit & KID"


def test_crawl_collects_image_manifest():
    images = {}
    corpus = la_crawl.crawl(adapter=FakeLA(), exclude_folders={"AI Agent"}, images_out=images)
    key = la_crawl._safe("SQUAT Cooldown Apr 14")   # the page (T_p2) with the embedded figure
    assert key in images, images
    assert images[key]["images"] == ["https://la/inline/plot.png"]
    assert images[key]["tree_id"] == "T_p2"
    # the <img> is stripped from the indexed TEXT (it's captured in the manifest instead)
    p2 = next(d for d in corpus if d["tree_id"] == "T_p2")
    assert "<img" not in p2["text"]


def test_excludes_ai_agent_folder():
    all_pages = {d["page"] for d in la_crawl.crawl(adapter=FakeLA())}
    assert "[UNSIGNED] some_report" in all_pages           # present when not excluded
    scoped = {d["page"] for d in la_crawl.crawl(adapter=FakeLA(), exclude_folders={"AI Agent"})}
    assert "[UNSIGNED] some_report" not in scoped          # AI Agent folder skipped
    assert "SQUAT Cooldown Apr 14" in scoped               # other folders still crawled


def test_max_pages_cap():
    assert len(la_crawl.crawl(adapter=FakeLA(), max_pages=1)) == 1


def test_notebook_filter():
    assert la_crawl.crawl(adapter=FakeLA(), notebooks=["Qubit & KID"])          # matches
    assert la_crawl.crawl(adapter=FakeLA(), notebooks=["Nonexistent"]) == []    # filtered out


def test_write_corpus_and_ask_finds_page():
    with tempfile.TemporaryDirectory() as tmp:
        la_crawl.KNOWLEDGE_ROOT = Path(tmp) / "knowledge"
        ask.KNOWLEDGE_ROOT = Path(tmp) / "knowledge"
        ask.OUTPUT_ROOT = Path(tmp) / "outputs"

        corpus = [{"notebook": "Qubit & KID", "page": "SQUAT Cooldown Apr 14",
                   "nbid": "NB1", "tree_id": "T_p2",
                   "text": "Fridge reached base temperature 12 mK; attenuation 90 dB."}]
        assert la_crawl.write_corpus(corpus) == 1
        f = (Path(tmp) / "knowledge" / "labarchives" / "SQUAT_Cooldown_Apr_14.md")
        assert f.exists() and "type: Lab Page" in f.read_text(encoding="utf-8")

        hits = ask.search("squat cooldown base temperature attenuation", k=5)
        assert hits and hits[0][1].startswith("la_page:"), hits


if __name__ == "__main__":
    import sys

    setup_module(None)
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
