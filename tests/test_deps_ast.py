"""Tests for WS5 AST-based dependency extraction (lab_agent.collect.dependencies).

Covers AST import parsing (multiline, aliases, magics, relative-skip), recovery of
code cells from serialized notebook content, and symbol-targeted source rendering.

Run: python -m pytest tests/test_deps_ast.py  (or: python tests/test_deps_ast.py)
"""
from lab_agent.collect.dependencies import (
    _extract_imports_ast,
    _code_blocks_from_serialized,
    _collect_imports_from_notebooks,
    _render_symbols,
)


def test_ast_imports_multiline_and_alias():
    src = (
        "import numpy as np\n"
        "from daq import Sweep, SweepPower\n"
        "from daq.analysis.mattis_bardeen import (\n    MB_fitter,\n    N_0,\n)\n"
    )
    imps = _extract_imports_ast(src)
    pkgs = {i.package for i in imps}
    assert "daq" in pkgs and "numpy" in pkgs
    mb = [i for i in imps if i.submodule == "analysis.mattis_bardeen"]
    assert mb and "MB_fitter" in mb[0].names and "N_0" in mb[0].names


def test_ast_imports_skip_magics_and_relative():
    src = "%matplotlib inline\n!pip install foo\nfrom . import sibling\nimport daq\n"
    imps = _extract_imports_ast(src)
    pkgs = {i.package for i in imps}
    assert "daq" in pkgs
    assert "" not in pkgs            # relative "from . import" skipped
    assert "sibling" not in pkgs


def test_code_blocks_recovered_from_serialized_notebook():
    content = (
        "## Cell 1 [markdown]\nsome notes about attenuation\n"
        "## Cell 2 [code] (execution_count=1)\nimport daq\nx = 1\n"
        "### Output\nimport evil_from_output_text\n"       # must NOT be seen as code
        "## Cell 3 [code] (execution_count=2)\nfrom presto import lockin\n"
    )
    blocks = _code_blocks_from_serialized(content)
    joined = "\n".join(blocks)
    assert "import daq" in joined and "from presto import lockin" in joined
    assert "evil_from_output_text" not in joined         # output text excluded

    imports = _collect_imports_from_notebooks(
        [type("A", (), {"path": "run.ipynb", "content": content})()]
    )
    assert "daq" in imports and "presto" in imports
    assert "evil_from_output_text" not in imports


def test_render_symbols_targets_imported_and_lists_constants():
    source = (
        '"""daq base module."""\n'
        "DAC_CURRENT = 40_500\n"
        "ADC_ATTENUATION = 0.0\n"
        "def amp_to_power_dbm(freq, amp):\n    return freq * amp\n"
        "def unused_helper():\n    return 1\n"
        "class Sweep:\n    pass\n"
    )
    out = _render_symbols(source, wanted={"amp_to_power_dbm", "Sweep"})
    assert "DAC_CURRENT = 40_500" in out                  # constant surfaced
    assert "def amp_to_power_dbm" in out                  # imported symbol full source
    assert "class Sweep" in out
    assert "unused_helper" in out                         # listed as a stub...
    assert "def unused_helper" not in out.split("Other top-level")[0]  # ...not full source


def test_render_symbols_none_on_syntax_error():
    assert _render_symbols("def (((broken", wanted=set()) is None


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
