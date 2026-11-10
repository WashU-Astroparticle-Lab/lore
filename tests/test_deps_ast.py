"""Tests for WS5 AST-based dependency extraction (lab_agent.collect.dependencies).

Covers AST import parsing (multiline, aliases, magics, relative-skip), recovery of
code cells from serialized notebook content, and symbol-targeted source rendering.

Run: python -m pytest tests/test_deps_ast.py  (or: python tests/test_deps_ast.py)
"""
from lab_agent.collect.dependencies import (
    _extract_imports_ast,
    _code_blocks_from_serialized,
    _collect_imports_from_notebooks,
    _follow_reexports,
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


def test_reexports_are_followed_to_the_module_that_defines_the_symbol():
    # `from daq import TimeStream` names no submodule, so only daq/__init__.py was fetched:
    # a re-export list. The real class two files down was never read.
    repo = {
        "measurements/__init__.py": "from .sweep import Sweep\nfrom .timestream import TimeStream as TS\n",
        "measurements/sweep.py": "class Sweep:\n    pass\n",
        "measurements/timestream.py": "class TimeStream:\n    '''Multi-tone.'''\n",
        "db/__init__.py": "def select_runs():\n    pass\n",
    }
    fetched = []

    def fetch(path):
        fetched.append(path)
        return repo.get(path)

    files = {"__init__.py": "from .measurements import Sweep, TS as TimeStream\nimport numpy\n"}
    wanted = {"Sweep", "TimeStream"}
    _follow_reexports(files, "daq", wanted, fetch)
    assert set(files) == {"__init__.py", "measurements/__init__.py", "measurements/sweep.py",
                          "measurements/timestream.py"}, sorted(files)
    assert "TS" in wanted, "a symbol imported under another name is looked up by its own"
    assert "class TimeStream" in _render_symbols(files["measurements/timestream.py"], wanted)
    assert "db/__init__.py" not in fetched, "nothing that was not imported is fetched"


def test_absolute_and_lazy_exports_are_followed_but_not_other_packages():
    # qpd re-exports with absolute imports, and its LED decoders load on first use.
    repo = {
        "reconstruction/static_bias.py": "def reconstruct_parity_flips_static(iq):\n    pass\n",
        "reconstruction/led_network/__init__.py":
            "from .reconstruct import reconstruct_parity_flips_led\n",
        "reconstruction/led_network/reconstruct.py":
            "def reconstruct_parity_flips_led(iq):\n    pass\n",
    }
    init = ("from qpd.reconstruction.static_bias import reconstruct_parity_flips_static\n"
            "from numpy.linalg import solve\n"
            "_LAZY = {'reconstruct_parity_flips_led'}\n"
            "def __getattr__(name):\n"
            "    from qpd.reconstruction import led_network\n"
            "    return getattr(led_network, name)\n")
    files = {"reconstruction/__init__.py": init}
    _follow_reexports(files, "qpd", {"reconstruct_parity_flips_static",
                                     "reconstruct_parity_flips_led", "solve"}, repo.get)
    assert "reconstruction/static_bias.py" in files, sorted(files)
    assert "reconstruction/led_network/reconstruct.py" in files, sorted(files)
    assert not any("numpy" in k for k in files), "an import from another package is not ours"

    # A symbol imported at the top is in __all__ as a string too; that is no lazy export.
    files = {"reconstruction/__init__.py": init.replace("_LAZY = {'reconstruct_parity_flips_led'}",
                                                        "__all__ = ['reconstruct_parity_flips_static']")}
    fetched = []
    _follow_reexports(files, "qpd", {"reconstruct_parity_flips_static"},
                      lambda p: fetched.append(p) or repo.get(p))
    assert fetched == ["reconstruction/static_bias.py"], fetched


def test_a_long_class_is_outlined_with_its_constructor():
    body = "\n".join(f"    def m{i}(self):\n        '''Step {i}.'''\n        return {i}"
                     for i in range(100))
    source = ("class TimeStream:\n    '''Multi-tone acquisition.'''\n"
              "    def __init__(self, freq, amp=0.1):\n        self.freq = freq\n" + body + "\n")
    out = _render_symbols(source, {"TimeStream"})
    assert "def __init__(self, freq, amp=0.1):" in out and "self.freq = freq" in out, out[:400]
    assert "def m7(...)  # Step 7." in out and "return 7" not in out, (
        "a several-hundred-line class is outlined, not pasted whole")


def test_the_repository_docs_about_each_symbol_are_attached():
    from lab_agent import repo_docs
    from lab_agent.collect import dependencies

    docs = {"README.md": "# daq\n\nWrappers for the Presto.\n\n## Install\n\npip\n",
            "daq/measurements/README.md": "# Measurements\n\n### 2. TimeStream (`timestream.py`)\n\n"
                                          "Multi-tone. Check `degenerate` before using flips.\n"}
    saved = repo_docs.doc_texts
    repo_docs.doc_texts = lambda owner, repo, token="": (docs, "13b74dd")
    try:
        lines = dependencies._documentation("lab", "daq", ["TimeStream", "Unknown"], "")
    finally:
        repo_docs.doc_texts = saved
    text = "\n".join(lines)
    assert "### Documentation (the repository's own docs, commit `13b74dd`)" in text, text
    assert "Wrappers for the Presto." in text
    assert ('#### `TimeStream`: daq/measurements/README.md, section "2. TimeStream '
            '(`timestream.py`)"') in text, text
    assert "Check `degenerate` before using flips." in text
    assert "Unknown" not in text, "a symbol the docs never mention gets no section"


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
