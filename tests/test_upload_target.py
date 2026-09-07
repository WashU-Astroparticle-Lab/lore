"""Stage A3 — upload picks the right report and reuses an existing page.

Regression targets from the 2026-08-26/31 review:
  * every upload created a new page, so four LabArchives pages ended up sharing
    the title "[UNSIGNED] presto_vna_spectrum_20260826";
  * the CLI took only <out_dir> and picked the newest "[UNSIGNED]" by mtime, so
    a directory holding both a full and a plain-language report was a coin flip;
  * upload returned a tree_id and nothing a human could follow, so the agent
    could not link its own upload and invented a URL that 404'd.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cli.upload import _parse_args
from lab_agent.publish.labarchives import (
    _find_page_in_folder,
    _notebook_url,
    resolve_report_path,
)

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


class FakeAdapter:
    """Stands in for LabArchivesAdapter — only _get_tree_level is used."""

    def __init__(self, titles: list[str], raises: bool = False) -> None:
        self.titles = titles
        self.raises = raises

    def _get_tree_level(self, nbid: str, tree_id: str):
        if self.raises:
            raise RuntimeError("network down")
        nodes = []
        for i, t in enumerate(self.titles):
            node = ET.Element("node")
            ET.SubElement(node, "display-text").text = t
            ET.SubElement(node, "tree-id").text = f"tree-{i}"
            nodes.append(node)
        return nodes


def test_resolve_report_path() -> None:
    d = Path(tempfile.mkdtemp())
    full = d / "[UNSIGNED] exp.md"
    simple = d / "[UNSIGNED] exp_simple.md"
    full.write_text("full", encoding="utf-8")
    time.sleep(0.01)
    simple.write_text("simple", encoding="utf-8")
    os.utime(simple, (time.time(), time.time()))

    check("newest [UNSIGNED] is the default", resolve_report_path(d).name == simple.name)
    check(
        "--report-file selects a specific report",
        resolve_report_path(d, "[UNSIGNED] exp.md").name == full.name,
    )
    check(
        "absolute --report-file works",
        resolve_report_path(d, str(full)).name == full.name,
    )

    try:
        resolve_report_path(d, "nope.md")
    except FileNotFoundError:
        check("a missing --report-file raises rather than silently falling back", True)
    else:
        raise AssertionError("FAILED: missing report file did not raise")

    empty = Path(tempfile.mkdtemp())
    try:
        resolve_report_path(empty)
    except FileNotFoundError:
        check("empty directory raises FileNotFoundError", True)
    else:
        raise AssertionError("FAILED: empty dir did not raise")

    # Fallback path when no [UNSIGNED] file exists.
    fb = Path(tempfile.mkdtemp())
    (fb / "experiment_report.md").write_text("x", encoding="utf-8")
    check("falls back to experiment_report.md", resolve_report_path(fb).name == "experiment_report.md")


def test_find_page_in_folder() -> None:
    title = "[UNSIGNED] presto_vna_spectrum_20260826"
    adapter = FakeAdapter(["Some other page", title, "Third"])
    check("existing page is found", _find_page_in_folder(adapter, "nb", "folder", title) == "tree-1")
    check(
        "match is case- and whitespace-insensitive",
        _find_page_in_folder(adapter, "nb", "folder", f"  {title.upper()} ") == "tree-1",
    )
    check(
        "absent page returns None so a new one is created",
        _find_page_in_folder(adapter, "nb", "folder", "Not There") is None,
    )
    check(
        "a lookup failure degrades to creating a page, not crashing",
        _find_page_in_folder(FakeAdapter([], raises=True), "nb", "folder", title) is None,
    )


def test_notebook_url() -> None:
    nbid = "MTQ5ODE5OC4wfDExNTI0NjAvMTE1MjQ2MC9Ob3RlYm9vay8xNDM5NDY5ODEzfDM4MDMxMTguMA=="
    url = _notebook_url(nbid)
    check("notebook url is built from the nbid", url == f"https://mynotebook.labarchives.com/{nbid}")
    check("notebook url has no fabricated page segment", "/page/" not in url)


def test_cli_args() -> None:
    target, opts = _parse_args(["outputs/exp", "--report-file", "[UNSIGNED] exp_simple.md"])
    check("out_dir positional survives", target == "outputs/exp")
    check("--report-file is read", opts["report_file"] == "[UNSIGNED] exp_simple.md")

    _, opts = _parse_args(["outputs/exp", "--page-title=My Page", "--new-page"])
    check("--page-title=value form works", opts["page_title"] == "My Page")
    check("--new-page sets the flag", opts["new_page"] is True)

    target, opts = _parse_args(["outputs/exp"])
    check(
        "defaults reuse the existing page",
        opts["new_page"] is False and opts["report_file"] is None and opts["page_title"] is None,
    )

    target, _ = _parse_args([])
    check("no arguments yields no target", target is None)


if __name__ == "__main__":
    test_resolve_report_path()
    test_find_page_in_folder()
    test_notebook_url()
    test_cli_args()
    print(f"\ntest_upload_target: {PASSED} checks passed")
