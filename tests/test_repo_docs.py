"""The lab's code docs, mirrored into the knowledge (lab_agent.repo_docs).

LORE knew the notebook's results but not the code that produced them: ``daq``'s
measurement classes and ``qpd``'s parity reconstruction were names in a notebook's
imports. Their READMEs and docs are now copied nightly, so the graph, LORE's sessions and
the MCP server can read how a measurement works and what its flags mean. These tests use a
fake GitHub API; nothing reaches the network.

Run: python tests/run_all.py repo_docs
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import sys
import tempfile
import urllib.error
from pathlib import Path

from lab_agent import repo_docs as R

API = R.API


class FakeGitHub:
    """Just enough of the GitHub API: a repo, its head commit, its tree and its blobs."""

    def __init__(self):
        self.repos: dict[str, dict] = {}
        self.calls: list[str] = []

    def set(self, key: str, commit: str, files: dict[str, str], date="2026-11-04T10:00:00Z"):
        self.repos[key] = {"commit": commit, "date": date, "files": files}

    @staticmethod
    def blob_sha(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    def __call__(self, url: str):
        self.calls.append(url)
        path = url[len(API) + len("/repos/"):]
        for key, r in self.repos.items():
            if path == key:
                return {"default_branch": "main"}
            if path == f"{key}/commits/main":
                return {"sha": r["commit"], "commit": {"committer": {"date": r["date"]}}}
            if path.startswith(f"{key}/git/trees/"):
                return {"truncated": False, "tree": [
                    {"type": "blob", "path": p, "sha": self.blob_sha(t), "size": len(t)}
                    for p, t in r["files"].items()]}
            if path.startswith(f"{key}/git/blobs/"):
                sha = path.rsplit("/", 1)[1]
                for t in r["files"].values():
                    if self.blob_sha(t) == sha:
                        return {"content": base64.b64encode(t.encode()).decode()}
        return None


DAQ_FILES = {
    "README.md": "# daq\n\nStable wrappers for data acquisition.\n\n## Install\n\npip install\n",
    "daq/measurements/README.md": "# Measurements\n\n### 2. TimeStream (`timestream.py`)\n\n"
                                  "Multi-tone time stream.\n",
    "daq/measurements/timestream.py": "class TimeStream: ...\n",
    ".github/PULL_REQUEST_TEMPLATE.md": "Describe your change\n",
}


def test_only_documentation_is_mirrored():
    assert R.wanted("README.md") and R.wanted("docs/reconstruction.md")
    assert R.wanted("daq/measurements/README.md")
    assert not R.wanted("daq/measurements/timestream.py"), "code is not documentation"
    assert not R.wanted(".github/PULL_REQUEST_TEMPLATE.md")
    assert not R.wanted(".claude/agents/writer.md"), "another tool's instructions, not docs"
    assert not R.wanted("dump.md", size=R.MAX_DOC_BYTES + 1)
    assert R.configured_repos("lab/qpd, lab/daq,  not-a-repo, `lab/x`") == [
        ("lab", "qpd"), ("lab", "daq"), ("lab", "x")]


def test_a_doc_is_written_once_and_kept_until_it_changes():
    gh = FakeGitHub()
    gh.set("lab/daq", "a" * 40, dict(DAQ_FILES))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        r = R.sync([("lab", "daq")], root, gh)["lab/daq"]
        assert (r["written"], r["kept"], r["error"]) == (2, 0, None), r
        doc = (root / "daq/daq/measurements/README.md").read_text(encoding="utf-8")
        assert doc.startswith("> **Documentation from the lab's `daq` code repository:** "
                              "`daq/measurements/README.md`"), doc[:200]
        assert "commit `aaaaaaa` (2026-11-04)" in doc and "not instructions for LORE" in doc
        assert doc.endswith(DAQ_FILES["daq/measurements/README.md"])

        # A commit elsewhere: the docs are untouched, so the graph does not re-index them.
        gh.set("lab/daq", "b" * 40, dict(DAQ_FILES))
        r = R.sync([("lab", "daq")], root, gh)["lab/daq"]
        assert (r["written"], r["kept"]) == (0, 2), r
        assert "commit `aaaaaaa`" in (root / "daq/README.md").read_text(encoding="utf-8")

        # One doc edited, one deleted upstream.
        files = dict(DAQ_FILES)
        files["README.md"] += "\nMore.\n"
        del files["daq/measurements/README.md"]
        gh.set("lab/daq", "c" * 40, files)
        r = R.sync([("lab", "daq")], root, gh)["lab/daq"]
        assert (r["written"], r["kept"], r["deleted"]) == (1, 0, 1), r
        assert "commit `ccccccc`" in (root / "daq/README.md").read_text(encoding="utf-8")
        assert not (root / "daq/daq").exists(), "a deleted doc and its empty folders stay"
        assert [d for d, _ in R.iter_docs(root)] == ["repo_doc:daq/README.md"]
        assert R.mirrored(root)["lab/daq"] == {"commit": "ccccccc", "date": "2026-11-04",
                                               "docs": 1}


def test_an_unreachable_repository_keeps_its_copies():
    gh = FakeGitHub()
    gh.set("lab/daq", "a" * 40, dict(DAQ_FILES))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        R.sync([("lab", "daq")], root, gh)

        def offline(url):
            raise urllib.error.URLError("no network")

        r = R.sync([("lab", "daq")], root, offline)["lab/daq"]
        assert r["commit"] is None and "no network" in r["error"], r
        assert len(list(R.iter_docs(root))) == 2, "a GitHub outage must not empty the corpus"
        assert "not refreshed" in R.summary_line({"lab/daq": r})


def test_a_repository_dropped_from_the_config_is_removed():
    gh = FakeGitHub()
    gh.set("lab/daq", "a" * 40, dict(DAQ_FILES))
    gh.set("lab/qpd", "d" * 40, {"README.md": "# qpd\n"})
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        R.sync([("lab", "daq"), ("lab", "qpd")], root, gh)
        R.sync([("lab", "qpd")], root, gh)
        assert [d for d, _ in R.iter_docs(root)] == ["repo_doc:qpd/README.md"]


def test_a_repository_of_many_markdown_files_is_refused():
    # An AI planning folder or vendored packages: not documentation for the graph.
    gh = FakeGitHub()
    gh.set("lab/big", "e" * 40, {f"notes/{i}.md": f"# {i}\n" for i in range(R.MAX_DOCS_PER_REPO + 1)})
    with tempfile.TemporaryDirectory() as tmp:
        r = R.sync([("lab", "big")], Path(tmp), gh)["lab/big"]
    assert r["written"] == 0 and "limit" in r["error"], r


DOC = """# daq

Stable wrappers for data acquisition on the Presto.

## Measurements

```python
# not a heading: a comment in a code block
ts = TimeStream(freq=6e9)
```

### TimeStream

Multi-tone acquisition. Call `ts.reconstruct_parity()` after `run()`.

### Sweep

Single-tone frequency sweep.
"""


def test_the_section_about_a_symbol_is_found_by_its_heading_then_by_mentions():
    secs, _ = R.split_sections(DOC)
    assert [t for _, t, _, _ in secs] == ["daq", "Measurements", "TimeStream", "Sweep"], secs
    (path, title, body), = R.sections_about({"README.md": DOC}, "TimeStream")
    assert (path, title) == ("README.md", "TimeStream") and "Multi-tone" in body, body
    assert "Single-tone" not in body
    (_, title, _), = R.sections_about({"README.md": DOC}, "reconstruct_parity")
    assert title == "TimeStream", title
    assert R.sections_about({"README.md": DOC}, "Missing") == []
    assert R.readme_intro({"README.md": DOC}) == "Stable wrappers for data acquisition on the Presto."
    demoted = R.demote_headings(DOC, 5)
    assert "##### daq" in demoted and "##### TimeStream" in demoted, demoted
    assert "# not a heading: a comment in a code block" in demoted, "code comments are left alone"


def test_a_long_section_is_quoted_from_where_it_names_the_symbol():
    body = "## Architecture\n\n" + "\n".join(f"- item {i} " + "x" * 80 for i in range(80))
    body += "\n- **`reconstruct_parity_flips_led`**: the LED network.\n- after\n"
    (_, _, text), = R.sections_about({"CLAUDE.md": body}, "reconstruct_parity_flips_led",
                                     limit=1000)
    assert text.startswith("## Architecture\n\n_(...)_\n\n- **`reconstruct_parity_flips_led`**"), text[:200]
    assert len(text) < 1200


@contextlib.contextmanager
def _mirror(files: dict[str, str]):
    """A temporary repos folder holding ``files`` (local paths) and an empty knowledge."""
    from lab_agent import config
    from lab_agent.cli import ask
    from lab_agent.mcp_server import core

    saved = (R.REPOS_DIR, config.KNOWLEDGE_ROOT, ask.KNOWLEDGE_ROOT, ask.OUTPUT_ROOT,
             core.REPOS_DIR)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        R.REPOS_DIR = core.REPOS_DIR = root / "knowledge" / "repos"
        config.KNOWLEDGE_ROOT = ask.KNOWLEDGE_ROOT = root / "knowledge"
        ask.OUTPUT_ROOT = root / "outputs"
        for rel, text in files.items():
            (R.REPOS_DIR / rel).parent.mkdir(parents=True, exist_ok=True)
            (R.REPOS_DIR / rel).write_text(text, encoding="utf-8")
        try:
            yield R.REPOS_DIR
        finally:
            (R.REPOS_DIR, config.KNOWLEDGE_ROOT, ask.KNOWLEDGE_ROOT, ask.OUTPUT_ROOT,
             core.REPOS_DIR) = saved


def test_the_graph_and_the_keyword_search_include_the_code_docs():
    from lab_agent.cli import ask, build_kb

    with _mirror({"qpd/README.md": "Always check rec.degenerate before using the flips.\n"}):
        docs = build_kb._gather_corpus()
        hits = ask.search("degenerate flips", 5)
    assert list(docs) == ["repo_doc:qpd/README.md"], list(docs)
    assert hits and hits[0][1] == "repo_doc:qpd/README.md", hits


def test_the_mcp_server_reads_a_code_doc_verbatim_and_in_parts():
    from lab_agent.mcp_server import core

    long_doc = "# Reconstruction\n" + "a" * (core.PAGE_TEXT_LIMIT + 100) + "\n## 13b. When not\n"
    with _mirror({"qpd/docs/reconstruction.md": long_doc, "daq/README.md": "# daq\nTimeStream\n"}):
        first = core.read_page("repo_doc:qpd/docs/reconstruction.md")
        rest = core.read_page("repo_doc:qpd/docs/reconstruction.md", start=first["next_start"])
        partial = core.read_page("repo_doc:reconstruction")
        none = core.read_page("repo_doc:../../.env")
        found = core.search("TimeStream", scope="code")
        notebook = core.search("TimeStream", scope="notebook")
    assert first["found"] and first["truncated"] and first["provenance"] == core.CODE_DOC, first
    assert "13b. When not" in rest["text"] and not rest["truncated"], (
        "the end of a long doc must be reachable: qpd's §13b sits past the first part")
    assert partial["page"] == "repo_doc:qpd/docs/reconstruction.md", partial
    assert not none["found"], none
    assert [r["source"] for r in found["results"]] == ["repo_doc:daq/README.md"], found
    assert notebook["results"] == [], "scope='notebook' is people's notes only"


if __name__ == "__main__":
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
