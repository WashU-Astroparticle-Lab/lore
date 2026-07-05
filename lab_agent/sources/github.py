from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from ..discover import build_artifact_group, synthesize_config, infer_title_from_notebook, infer_objective_from_notebook
from ..ingest import (
    TEXT_SUFFIXES,
    csv_rows_from_text,
    csv_table_from_rows,
    iter_all_artifacts,
    notebook_markdown_from_content,
    notebook_text_from_content,
)
from ..models import Artifact, CollectedArtifact, ExperimentBundle

# Matches: https://github.com/<owner>/<repo>/tree/<ref>[/<path>]
_GITHUB_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/tree/(?P<ref>[^/]+)(?:/(?P<path>.+))?"
)


def parse_github_url(url: str) -> tuple[str, str, str, str]:
    """Parse a GitHub folder URL into (owner, repo, ref, folder_path).

    The ``ref`` may be a commit SHA (preferred, for permanence) or a branch
    name.  The ``folder_path`` is the path within the repo to the experiment
    folder; it is an empty string when the URL points to the repo root.

    Raises ``ValueError`` if the URL does not match the expected format.
    """
    m = _GITHUB_URL_RE.match(url.rstrip("/"))
    if not m:
        raise ValueError(
            f"Cannot parse GitHub URL: {url!r}\n"
            "Expected format: https://github.com/<owner>/<repo>/tree/<ref>[/<path>]"
        )
    return (
        m.group("owner"),
        m.group("repo"),
        m.group("ref"),
        (m.group("path") or "").rstrip("/"),
    )


class GitHubAdapter:
    """Source adapter that ingests an experiment from a GitHub folder URL.

    Artifact discovery is driven entirely by the actual folder contents.
    Title and objective are inferred from the primary notebook's markdown cells.

    URL format::

        https://github.com/<owner>/<repo>/tree/<ref>[/<path>]

    Authentication
    --------------
    Public repos need no token.  For private repos supply a GitHub personal
    access token via the ``token`` parameter or the ``GITHUB_TOKEN`` env var.
    """

    def __init__(self, url: str, token: str | None = None) -> None:
        self._url = url
        self._token = token or os.environ.get("GITHUB_TOKEN")
        self.owner, self.repo, self.ref, self.folder_path = parse_github_url(url)

    # ------------------------------------------------------------------
    # Low-level fetch helpers
    # ------------------------------------------------------------------

    def _raw_url(self, relative_path: str) -> str:
        """raw.githubusercontent.com URL for a file inside the experiment folder."""
        parts = [p for p in [self.folder_path, relative_path] if p]
        path = urllib.parse.quote("/".join(parts), safe="/")
        return f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.ref}/{path}"

    def _fetch(self, relative_path: str) -> str | None:
        """Fetch a file's text content.  Returns None on 404."""
        raw = self._fetch_bytes(relative_path)
        return raw.decode("utf-8", errors="replace") if raw is not None else None

    def _fetch_bytes(self, relative_path: str) -> bytes | None:
        """Fetch a file's raw bytes.  Returns None on 404."""
        req = urllib.request.Request(self._raw_url(relative_path))
        if self._token:
            req.add_header("Authorization", f"token {self._token}")
        req.add_header("User-Agent", "lab-agent/1.0")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def _fetch_api(self, api_path: str) -> object:
        """Fetch from the GitHub API and return the parsed JSON object."""
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/{api_path}"
        req = urllib.request.Request(url)
        if self._token:
            req.add_header("Authorization", f"token {self._token}")
        req.add_header("User-Agent", "lab-agent/1.0")
        req.add_header("Accept", "application/vnd.github.v3+json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"GitHub API error {exc.code} for {url}: {exc.reason}"
            ) from exc

    # ------------------------------------------------------------------
    # Folder discovery
    # ------------------------------------------------------------------

    def _list_folder(self) -> list[str]:
        """Return all file paths in the experiment folder, relative to its root.

        Uses the Git Trees API with recursive=1 (one API call for the whole
        tree) then filters to the experiment folder prefix.
        """
        data = self._fetch_api(f"git/trees/{self.ref}?recursive=1")
        prefix = self.folder_path + "/" if self.folder_path else ""
        paths: list[str] = []
        for item in data.get("tree", []):
            if item.get("type") != "blob":
                continue
            item_path: str = item["path"]
            if prefix:
                if item_path.startswith(prefix):
                    paths.append(item_path[len(prefix):])
            else:
                paths.append(item_path)
        if data.get("truncated"):
            print(
                f"[GitHub] WARNING: tree response was truncated for {self._url}. "
                "Some files may be missing from the report. "
                "Consider pointing to a subfolder instead of the repo root.",
                flush=True,
            )
        return paths

    # ------------------------------------------------------------------
    # Artifact collection
    # ------------------------------------------------------------------

    _IMAGE_SUFFIXES = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
    _BINARY_SUFFIXES = frozenset({
        "svg", "pdf", "pkl", "npy", "npz", "hdf5", "h5", "parquet", "feather",
    })

    def _collect(self, artifact: Artifact) -> CollectedArtifact:
        """Fetch and parse one artifact from GitHub."""
        suffix = artifact.path.lower().rsplit(".", 1)[-1] if "." in artifact.path else ""

        if suffix in self._IMAGE_SUFFIXES:
            print(f"[GitHub] Downloading image: {artifact.path}")
            raw = self._fetch_bytes(artifact.path)
            return CollectedArtifact(
                path=artifact.path, kind=artifact.type,
                description=artifact.description, exists=raw is not None,
                raw_bytes=raw, source="github",
            )

        if suffix in self._BINARY_SUFFIXES:
            return CollectedArtifact(
                path=artifact.path, kind=artifact.type,
                description=artifact.description, exists=True, source="github",
            )

        text = self._fetch(artifact.path)
        if text is None:
            return CollectedArtifact(
                path=artifact.path, kind=artifact.type,
                description=artifact.description, exists=False, source="github",
            )

        markdown_cells: list[str] = []
        csv_rows: list[list[str]] = []
        content: str | None = None

        if artifact.type == "notebook" and suffix == "ipynb":
            try:
                content = notebook_text_from_content(text)
                markdown_cells = notebook_markdown_from_content(text)
            except Exception as exc:
                print(f"[GitHub] Warning: could not parse notebook {artifact.path!r}: {exc}", flush=True)
                content = text  # fall back to raw JSON so Claude still has the content
                markdown_cells = []
        elif suffix in ("csv", "tsv"):
            csv_rows = csv_rows_from_text(text)
            content = csv_table_from_rows(csv_rows)
        elif f".{suffix}" in TEXT_SUFFIXES or suffix in ("py", "r", "jl", "m", "sh"):
            content = text

        return CollectedArtifact(
            path=artifact.path, kind=artifact.type,
            description=artifact.description, exists=True,
            content=content, markdown_cells=markdown_cells,
            csv_rows=csv_rows, source="github",
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def load(self) -> ExperimentBundle:
        """Discover and fetch all artifacts in the GitHub folder.

        Discovery flow
        --------------
        1. List the full folder tree via the Git Trees API.
        2. Classify every file into an artifact role (notebook / raw_data / …).
        3. Collect all artifact content (fetches notebooks, CSVs, notes, …).
        4. Infer title + objective from the primary notebook's markdown cells.
        5. Re-rank notebooks by code cell count if needed.
        6. Build final config from notebook inference with folder-name fallback.
        """
        folder_name = self.folder_path.rstrip("/").rsplit("/", 1)[-1] or self.repo

        # Steps 1–2 — discover and classify all files
        file_paths = self._list_folder()
        print(f"[GitHub] folder_name={folder_name!r}")
        print(f"[GitHub] {len(file_paths)} file(s) discovered:")
        for p in file_paths:
            print(f"  {p}")
        artifact_group = build_artifact_group(file_paths, folder_name=folder_name)

        # Step 3 — provisional config for iter_all_artifacts
        provisional_config = synthesize_config(folder_name, artifact_group)

        # Step 4 — collect all artifact content
        collected = [self._collect(art) for art in iter_all_artifacts(provisional_config)]

        # Step 5 — re-rank primary notebook by code cell count
        def _code_cells(path: str) -> int:
            art = next((a for a in collected if a.path == path and a.kind == "notebook"), None)
            return (art.content or "").count("[code]") if art else 0

        if artifact_group.primary_execution and artifact_group.supporting_context:
            primary_count = _code_cells(artifact_group.primary_execution[0].path)
            best = max(
                (a for a in artifact_group.supporting_context if a.type == "notebook"),
                key=lambda a: _code_cells(a.path),
                default=None,
            )
            if best is not None and _code_cells(best.path) > primary_count:
                old_primary = artifact_group.primary_execution[0]
                artifact_group.primary_execution[0] = best
                artifact_group.supporting_context.remove(best)
                artifact_group.supporting_context.insert(0, old_primary)
                print(f"[GitHub] Re-ranked primary notebook: {best.path!r} ({_code_cells(best.path)} code cells) over {old_primary.path!r} ({primary_count})")

        # Step 6 — infer title and objective from primary notebook markdown cells
        inferred_title: str | None = None
        inferred_objective: str | None = None
        primary_nb = next(
            (a for a in collected if a.kind == "notebook" and a.exists and a.markdown_cells),
            None,
        )
        if primary_nb:
            inferred_title = infer_title_from_notebook(primary_nb.markdown_cells)
            inferred_objective = infer_objective_from_notebook(primary_nb.markdown_cells)

        config = synthesize_config(
            folder_name,
            artifact_group,
            inferred_title=inferred_title,
            inferred_objective=inferred_objective,
        )

        return ExperimentBundle(
            root_dir=self._url,
            config=config,
            collected_artifacts=collected,
        )
