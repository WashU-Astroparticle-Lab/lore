"""Lightweight summaries of binary data artifacts (S2 ingestion completeness).

Turns opaque binary data files (previously just noted as "exists") into a short text
description the analysts can read: array shape/dtype for ``.npy``, member arrays for
``.npz``, and dataset names/shapes for HDF5. All introspection is guarded — any
failure or a missing optional dependency (numpy/h5py) degrades to a type+size note,
never an error.
"""
from __future__ import annotations

import io
import zipfile

# Data-bearing binaries worth introspecting (not figures like .pdf/.svg).
DATA_BINARY_SUFFIXES = frozenset({"npy", "npz", "hdf5", "h5", "pkl", "parquet", "feather"})


def _fmt_size(n: int) -> str:
    kb = n / 1024
    return f"{kb:.0f} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"


def _summarize_npy(data: bytes) -> str:
    from numpy.lib import format as npf
    f = io.BytesIO(data)
    version = npf.read_magic(f)
    if version == (1, 0):
        shape, _fortran, dtype = npf.read_array_header_1_0(f)
    elif version == (2, 0):
        shape, _fortran, dtype = npf.read_array_header_2_0(f)
    else:
        shape, _fortran, dtype = npf._read_array_header(f, version)
    return f"NumPy array, shape={shape}, dtype={dtype}"


def _summarize_npz(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = [n[:-4] if n.endswith(".npy") else n for n in zf.namelist()]
    shown = ", ".join(names[:12]) + (f" (+{len(names) - 12} more)" if len(names) > 12 else "")
    return f"NumPy .npz archive, {len(names)} array(s): {shown}"


def _summarize_hdf5(data: bytes) -> str:
    import h5py
    lines: list[str] = []
    with h5py.File(io.BytesIO(data), "r") as f:
        def visit(name, obj):
            if isinstance(obj, h5py.Dataset):
                lines.append(f"  {name}: shape={obj.shape}, dtype={obj.dtype}")
        f.visititems(visit)
    if not lines:
        return "HDF5 file (no datasets found)"
    more = f"\n  … (+{len(lines) - 30} more datasets)" if len(lines) > 30 else ""
    return "HDF5 datasets:\n" + "\n".join(lines[:30]) + more


def summarize_binary(path: str, data: bytes) -> str:
    """Return a short human-readable summary of a binary data file."""
    suffix = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    size = _fmt_size(len(data))
    try:
        if suffix == "npy":
            return f"{_summarize_npy(data)} ({size})"
        if suffix == "npz":
            return f"{_summarize_npz(data)} ({size})"
        if suffix in ("h5", "hdf5"):
            return f"{_summarize_hdf5(data)}\n({size})"
    except Exception as exc:  # noqa: BLE001 — introspection is best-effort
        return f"binary {suffix or '?'} file, {size} — could not introspect ({type(exc).__name__})"
    return f"binary {suffix or '?'} file, {size} — not introspected"
