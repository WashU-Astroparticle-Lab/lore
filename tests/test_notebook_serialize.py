"""Tests for the WS1 notebook serializer (lab_agent.collect.ingest.serialize_notebook).

Covers code source, execution_count in the header, the three output kinds
(stream / execute_result+image / error), image extraction, ANSI stripping, and the
per-output truncation cap. No credentials or network needed.

Run: python -m pytest tests/test_notebook_serialize.py  (or: python tests/test_notebook_serialize.py)
"""
import base64
import json

from lab_agent.collect.ingest import serialize_notebook, notebook_text_from_content

_PNG = b"\x89PNG\r\n\x1a\nFAKEDATA"
_PNG_B64 = base64.b64encode(_PNG).decode()


def _nb(cells: list[dict]) -> str:
    return json.dumps({"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5})


def _md(source):
    return {"cell_type": "markdown", "metadata": {}, "source": source}


def _code(source, execution_count, outputs):
    return {"cell_type": "code", "metadata": {}, "execution_count": execution_count,
            "source": source, "outputs": outputs}


_FULL = _nb([
    _md(["# Title\n", "intro text"]),
    _code("print('hi')", 1, [{"output_type": "stream", "name": "stdout", "text": "hello world\n"}]),
    _code("plot()", 2, [{"output_type": "execute_result", "metadata": {}, "execution_count": 2,
                         "data": {"text/plain": "<Figure size>", "image/png": _PNG_B64}}]),
    _code("boom()", None, [{"output_type": "error", "ename": "NameError",
                            "evalue": "name 'boom' is not defined",
                            "traceback": ["[31m--- Traceback ---[0m",
                                          "  File x, line 1", "NameError: name 'boom' is not defined"]}]),
])


def test_code_and_outputs_serialized():
    content, images = serialize_notebook(_FULL, image_prefix="nb_")
    assert "print('hi')" in content                       # code source present
    assert "## Cell 2 [code] (execution_count=1)" in content
    assert "hello world" in content                        # stream output
    assert "<Figure size>" in content                      # execute_result text/plain
    assert "## Cell 1 [markdown]" in content and "intro text" in content


def test_image_output_extracted_with_marker():
    content, images = serialize_notebook(_FULL, image_prefix="nb_")
    assert images == [("nb_cell3_out0.png", _PNG)], images
    assert "[output image: github_images/nb_cell3_out0.png]" in content


def test_unexecuted_cell_and_error_ansi_stripped():
    content, _ = serialize_notebook(_FULL)
    assert "## Cell 4 [code] (unexecuted)" in content      # execution_count None
    assert "NameError: name 'boom' is not defined" in content
    assert "\x1b[" not in content                          # ANSI escapes removed


def test_output_truncation_cap():
    big = "x" * 9000
    nb = _nb([_code("print(big)", 1, [{"output_type": "stream", "name": "stdout", "text": big}])])
    content, _ = serialize_notebook(nb)
    assert "[truncated" in content
    assert len(content) < len(big) + 500


def test_no_images_when_none_present():
    nb = _nb([_code("x=1", 1, [{"output_type": "stream", "name": "stdout", "text": "ok\n"}])])
    content, images = serialize_notebook(nb)
    assert images == []
    assert "[output image" not in content


def test_wrapper_returns_text_only():
    text = notebook_text_from_content(_FULL)
    assert isinstance(text, str) and "print('hi')" in text


def test_github_collect_yields_notebook_and_image_artifacts():
    # Integration: _collect on a notebook returns the notebook artifact plus one
    # 'figure' artifact per cell-output image, served from the tarball cache (no net).
    from lab_agent.sources.github import GitHubAdapter
    from lab_agent.models import Artifact

    adapter = GitHubAdapter.__new__(GitHubAdapter)   # bypass __init__ (no URL/network)
    adapter._token = None
    adapter._file_cache = {"run.ipynb": _FULL.encode("utf-8")}

    arts = adapter._collect(Artifact(path="run.ipynb", type="notebook", description=None))
    nbs = [a for a in arts if a.kind == "notebook"]
    figs = [a for a in arts if a.kind == "figure"]
    assert len(nbs) == 1 and nbs[0].content and "print('hi')" in nbs[0].content
    assert len(figs) == 1
    assert figs[0].raw_bytes == _PNG and figs[0].source == "github"
    assert figs[0].path == "run_cell3_out0.png"   # bare filename → saved into github_images/


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
