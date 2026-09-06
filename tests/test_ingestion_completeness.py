"""Tests for Stage 2 ingestion completeness: standalone .py scanning + binary summaries.

Run: python -m pytest tests/test_ingestion_completeness.py
     (or: python tests/test_ingestion_completeness.py)
"""
import io
import zipfile

from lab_agent.collect.dependencies import _collect_imports_from_notebooks
from lab_agent.collect.binary import summarize_binary


class _Art:
    def __init__(self, path, content):
        self.path = path
        self.content = content


def test_py_scripts_scanned_for_imports():
    py = _Art("analysis.py", "import numpy as np\nfrom daq import Sweep\nx = Sweep()\n")
    nb = _Art("run.ipynb", "## Cell 1 [code] (execution_count=1)\nfrom presto import lockin\n")
    imports = _collect_imports_from_notebooks([py, nb])
    assert "daq" in imports          # from the standalone .py
    assert "presto" in imports       # from the notebook
    assert "numpy" not in imports    # stdlib/well-known skipped


def test_summarize_npy():
    try:
        import numpy as np
    except ImportError:
        return  # numpy optional; skip if absent
    buf = io.BytesIO()
    np.save(buf, np.zeros((3, 4), dtype=np.float64))
    out = summarize_binary("arr.npy", buf.getvalue())
    assert "NumPy array" in out and "shape=(3, 4)" in out and "float64" in out


def test_summarize_npz():
    try:
        import numpy as np
    except ImportError:
        return
    buf = io.BytesIO()
    np.savez(buf, alpha=np.zeros(2), beta=np.ones(3))
    out = summarize_binary("d.npz", buf.getvalue())
    assert ".npz archive" in out and "alpha" in out and "beta" in out


def test_summarize_unknown_binary_notes_size():
    out = summarize_binary("model.pkl", b"\x80\x04" + b"x" * 5000)
    assert "pkl" in out and "KB" in out and "not introspected" in out


def test_summarize_corrupt_npy_degrades_gracefully():
    out = summarize_binary("bad.npy", b"not a real npy file")
    assert "could not introspect" in out and "KB" in out


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
