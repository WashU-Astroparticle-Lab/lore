"""
lab_agent/dependencies.py — resolve lab-specific imports from experiment notebooks.

For each notebook in the experiment, scans import statements and classifies them:
  - Standard / well-known third-party packages → skipped
  - Unknown packages → searched in the same GitHub org as the experiment repo

For packages found in the org, fetches __init__.py and the specific submodules
that were actually imported, so Claude understands what those dependencies do.

Output: dependencies.md in the experiment output folder.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Well-known packages to skip — not worth fetching
# ---------------------------------------------------------------------------

_SKIP_PACKAGES = frozenset({
    # Python stdlib
    "os", "sys", "re", "io", "abc", "ast", "copy", "csv", "math", "time",
    "json", "uuid", "enum", "glob", "gzip", "hmac", "http", "logging",
    "socket", "struct", "shutil", "signal", "string", "typing", "hashlib",
    "pathlib", "datetime", "functools", "itertools", "operator", "warnings",
    "traceback", "threading", "multiprocessing", "collections", "contextlib",
    "dataclasses", "importlib", "subprocess",
    # Common scientific / data stack
    "numpy", "np", "scipy", "matplotlib", "pandas", "pd", "sklearn",
    "sklearn", "statsmodels", "sympy", "numba", "cython",
    # Jupyter / plotting
    "IPython", "ipywidgets", "ipython", "notebook", "tqdm",
    "seaborn", "plotly", "bokeh", "altair",
    # Utilities
    "h5py", "hdf5", "yaml", "toml", "dotenv", "cloudpickle", "pickle",
    "requests", "urllib", "httpx", "aiohttp",
    "PIL", "cv2", "skimage",
    "pydantic", "attrs", "click", "rich",
})


class ImportInfo(NamedTuple):
    package: str          # top-level package name
    submodule: str        # e.g. "hardware" from "from presto.hardware import ..."
    names: list[str]      # specific names imported, if any


# ---------------------------------------------------------------------------
# Import extraction
# ---------------------------------------------------------------------------

def _extract_imports(source: str) -> list[ImportInfo]:
    """Extract (package, submodule, names) from Python source."""
    results: list[ImportInfo] = []
    for line in source.splitlines():
        line = line.strip()
        # from x.y.z import a, b, c
        m = re.match(r"from\s+([\w.]+)\s+import\s+(.+)", line)
        if m:
            full_mod = m.group(1)
            names = [n.strip().split(" as ")[0].strip() for n in m.group(2).split(",")]
            parts = full_mod.split(".")
            results.append(ImportInfo(parts[0], ".".join(parts[1:]) if len(parts) > 1 else "", names))
            continue
        # import x.y, z
        m = re.match(r"import\s+(.+)", line)
        if m:
            for item in m.group(1).split(","):
                item = item.strip().split(" as ")[0].strip()
                parts = item.split(".")
                results.append(ImportInfo(parts[0], "", []))
    return results


def _collect_imports_from_notebooks(bundle_artifacts) -> dict[str, list[ImportInfo]]:
    """Return {package: [ImportInfo, ...]} for all non-standard imports in notebooks.

    Artifacts store notebook content as flat text (all cells labelled by type),
    so we scan the text directly for import lines rather than parsing JSON.
    """
    all_imports: dict[str, list[ImportInfo]] = {}
    for art in bundle_artifacts:
        if not (art.path.endswith(".ipynb") and art.content):
            continue
        for imp in _extract_imports(art.content):
            if imp.package in _SKIP_PACKAGES:
                continue
            if imp.package not in all_imports:
                all_imports[imp.package] = []
            all_imports[imp.package].append(imp)
    return all_imports


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _gh_get(url: str, token: str) -> dict | list | None:
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "lab-agent/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (404, 403):
            return None
        raise


def _gh_file(url: str, token: str) -> str | None:
    data = _gh_get(url, token)
    if not data or "content" not in data:
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:
        return None


def _list_org_repos(owner: str, token: str) -> dict[str, str]:
    """Return {repo_name_lower: default_branch} for the org."""
    repos = {}
    page = 1
    while True:
        url = f"https://api.github.com/orgs/{owner}/repos?per_page=100&page={page}"
        data = _gh_get(url, token)
        if not data:
            break
        for r in data:
            repos[r["name"].lower()] = r.get("default_branch", "main")
        if len(data) < 100:
            break
        page += 1
    return repos


def _fetch_package_source(
    owner: str,
    repo: str,
    branch: str,
    package: str,
    submodules: list[str],
    token: str,
) -> dict[str, str]:
    """Fetch __init__.py and any directly-imported submodule files."""
    base = f"https://api.github.com/repos/{owner}/{repo}/contents"
    fetched: dict[str, str] = {}

    # Try package at root or in src/
    for pkg_root in [package, f"src/{package}", repo, f"src/{repo}"]:
        init_url = f"{base}/{pkg_root}/__init__.py"
        content = _gh_file(init_url, token)
        if content:
            fetched["__init__.py"] = content
            # Fetch each submodule file
            for sub in submodules:
                if not sub:
                    continue
                sub_path = sub.replace(".", "/")
                for ext in [".py", "/__init__.py"]:
                    sub_url = f"{base}/{pkg_root}/{sub_path}{ext}"
                    sub_content = _gh_file(sub_url, token)
                    if sub_content:
                        fetched[f"{sub_path}{ext}"] = sub_content
                        break
            break

    # If no package dir found, try a single-file module
    if not fetched:
        for candidate in [f"{package}.py", f"src/{package}.py"]:
            content = _gh_file(f"{base}/{candidate}", token)
            if content:
                fetched[f"{package}.py"] = content
                break

    return fetched


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_dependencies(
    out_dir: Path,
    bundle_artifacts,
    owner: str,
) -> None:
    """Scan notebooks for lab-specific imports, find them in the GitHub org,
    fetch relevant source, and write dependencies.md to out_dir.

    Args:
        out_dir: Experiment output directory.
        bundle_artifacts: All collected artifacts from the bundle.
        owner: GitHub org/user to search (e.g. "WashU-Astroparticle-Lab").
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("[deps] No GITHUB_TOKEN — skipping dependency resolution.")
        return

    print(f"[deps] Scanning notebooks for lab-specific imports…")
    imports = _collect_imports_from_notebooks(bundle_artifacts)

    if not imports:
        print("[deps] No lab-specific imports found.")
        return

    print(f"[deps] Found candidate packages: {', '.join(sorted(imports))}")

    # List all repos in the org once
    org_repos = _list_org_repos(owner, token)

    lines: list[str] = [
        "# Dependency Source Code",
        "",
        "Lab-specific packages imported by the experiment notebooks.",
        "Standard libraries (numpy, scipy, etc.) are omitted.",
        "",
    ]

    for package, import_list in sorted(imports.items()):
        # Collect unique submodules imported
        submodules = list(dict.fromkeys(
            imp.submodule for imp in import_list if imp.submodule
        ))
        imported_names = list(dict.fromkeys(
            name for imp in import_list for name in imp.names if name
        ))

        lines.append(f"## `{package}`")
        lines.append("")

        # Check if it's in the org
        repo_name = None
        for candidate in [package, package.replace("_", "-"), package.replace("-", "_")]:
            if candidate.lower() in org_repos:
                repo_name = next(k for k in org_repos if k.lower() == candidate.lower())
                break

        if repo_name is None:
            lines.append(f"**Source:** Not found in `{owner}` — likely an external package.")
            lines.append("")
            seen: set[str] = set()
            for imp in import_list:
                if imp.submodule and imp.names:
                    stmt = f"from {imp.package}.{imp.submodule} import {', '.join(imp.names)}"
                elif imp.names:
                    stmt = f"from {imp.package} import {', '.join(imp.names)}"
                else:
                    stmt = f"import {imp.package}"
                if stmt not in seen:
                    seen.add(stmt)
                    lines.append(f"- `{stmt}`")
            lines.append("")
            continue

        branch = org_repos[repo_name.lower()]
        lines.append(f"**Source:** `{owner}/{repo_name}` (branch: `{branch}`)")
        lines.append("")

        if imported_names:
            lines.append(f"**Symbols used:** `{'`, `'.join(imported_names)}`")
            lines.append("")

        print(f"[deps] Fetching source for `{package}` from {owner}/{repo_name}…")
        source_files = _fetch_package_source(owner, repo_name, branch, package, submodules, token)

        if not source_files:
            lines.append("_Source files could not be retrieved._")
            lines.append("")
            continue

        for filename, content in source_files.items():
            # Truncate very long files to keep dependencies.md readable
            content_lines = content.splitlines()
            truncated = False
            if len(content_lines) > 200:
                content_lines = content_lines[:200]
                truncated = True
            lines.append(f"### `{filename}`")
            lines.append("")
            lines.append("```python")
            lines.extend(content_lines)
            if truncated:
                lines.append(f"# ... ({len(content.splitlines()) - 200} more lines truncated)")
            lines.append("```")
            lines.append("")

    output = "\n".join(lines)
    (out_dir / "dependencies.md").write_text(output, encoding="utf-8")
    print(f"[deps] Saved dependencies.md ({len(output):,} chars)")
