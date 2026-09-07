from __future__ import annotations

import io
import json
import os
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from ..collect.discover import build_artifact_group, synthesize_config, infer_title_from_notebook, infer_objective_from_notebook
from ..collect.binary import DATA_BINARY_SUFFIXES as _DATA_BINARY_SUFFIXES, summarize_binary
from ..collect.ingest import (
    TEXT_SUFFIXES,
    csv_rows_from_text,
    csv_table_from_rows,
    iter_all_artifacts,
    notebook_markdown_from_content,
    serialize_notebook,
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

    # Refuse tarballs larger than this and fall back to per-file fetching.
    _MAX_TARBALL_BYTES = 200 * 1024 * 1024

    def __init__(self, url: str, token: str | None = None) -> None:
        self._url = url
        self._token = token or os.environ.get("GITHUB_TOKEN")
        self.owner, self.repo, self.ref, self.folder_path = parse_github_url(url)
        # {relative_path: bytes} snapshot of the experiment folder, populated by
        # _load_tarball(). When set, _fetch_bytes serves from it with no HTTP.
        self._file_cache: dict[str, bytes] | None = None
        # Provenance (WS2), populated during load().
        self.commit_sha: str | None = None
        self.commit_date: str | None = None
        self.tree_truncated: bool = False

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
        """Fetch a file's raw bytes.  Returns None on 404.

        Serves from the tarball snapshot when one was loaded — the tarball is a
        complete snapshot of the ref, so a cache miss is equivalent to a 404.
        """
        if self._file_cache is not None:
            return self._file_cache.get(relative_path)
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

    def _load_tarball(self) -> bool:
        """Download the repo tarball once and index the experiment folder's files.

        One HTTP request replaces a request per file. Returns True on success;
        on any failure (network, size cap, parse) leaves the cache unset so
        _fetch_bytes falls back to per-file fetching.
        """
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/tarball/{self.ref}"
        req = urllib.request.Request(url)
        if self._token:
            req.add_header("Authorization", f"token {self._token}")
        req.add_header("User-Agent", "lab-agent/1.0")
        try:
            chunks: list[bytes] = []
            size = 0
            with urllib.request.urlopen(req, timeout=120) as resp:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self._MAX_TARBALL_BYTES:
                        print(f"[GitHub] Tarball exceeds {self._MAX_TARBALL_BYTES // 2**20} MB; "
                              "falling back to per-file downloads.")
                        return False
                    chunks.append(chunk)
            data = b"".join(chunks)

            files: dict[str, bytes] = {}
            prefix = self.folder_path + "/" if self.folder_path else ""
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    # Tarball paths start with "<owner>-<repo>-<sha>/"
                    rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
                    if prefix:
                        if not rel.startswith(prefix):
                            continue
                        rel = rel[len(prefix):]
                    handle = tf.extractfile(member)
                    if handle is not None:
                        files[rel] = handle.read()
            self._file_cache = files
            print(f"[GitHub] Tarball snapshot loaded: {len(files)} file(s), {size // 1024} KB")
            return True
        except Exception as exc:
            print(f"[GitHub] Tarball fetch failed ({exc}); falling back to per-file downloads.")
            return False

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
            self.tree_truncated = True
            print(
                f"[GitHub] WARNING: tree response was truncated for {self._url}. "
                "Some files may be missing from the report. "
                "Consider pointing to a subfolder instead of the repo root.",
                flush=True,
            )
        return paths

    def _resolve_commit(self) -> None:
        """Resolve the ref to a concrete commit SHA + date via one API call.

        Best-effort: on any failure the provenance fields stay None and the
        pipeline proceeds. Stamped onto every collected artifact so citations and
        freshness checks can name the exact snapshot the numbers came from.
        """
        try:
            data = self._fetch_api(f"commits/{self.ref}")
        except Exception as exc:  # noqa: BLE001 — provenance is non-fatal
            print(f"[GitHub] Could not resolve commit for {self.ref!r}: {exc}")
            return
        if isinstance(data, dict):
            self.commit_sha = data.get("sha")
            author = (data.get("commit") or {}).get("author") or {}
            self.commit_date = author.get("date")

    # ------------------------------------------------------------------
    # Artifact collection
    # ------------------------------------------------------------------

    _IMAGE_SUFFIXES = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
    _BINARY_SUFFIXES = frozenset({
        "svg", "pdf", "pkl", "npy", "npz", "hdf5", "h5", "parquet", "feather",
    })

    def _collect(self, artifact: Artifact) -> list[CollectedArtifact]:
        """Fetch and parse one artifact from GitHub.

        Returns a list because a notebook yields its own artifact plus one
        ``figure`` artifact per code-cell output image (saved alongside committed
        images so the analysts can open the actual result plots).
        """
        suffix = artifact.path.lower().rsplit(".", 1)[-1] if "." in artifact.path else ""

        if suffix in self._IMAGE_SUFFIXES:
            print(f"[GitHub] Downloading image: {artifact.path}")
            raw = self._fetch_bytes(artifact.path)
            return [CollectedArtifact(
                path=artifact.path, kind=artifact.type,
                description=artifact.description, exists=raw is not None,
                raw_bytes=raw, source="github",
            )]

        if suffix in self._BINARY_SUFFIXES:
            # Data binaries (.npy/.npz/.h5/…) get a short shape/dtype summary so they
            # aren't opaque to the analysts (S2); figures (.pdf/.svg) stay as-is.
            if suffix in _DATA_BINARY_SUFFIXES:
                raw = self._fetch_bytes(artifact.path)
                content = summarize_binary(artifact.path, raw) if raw is not None else None
                return [CollectedArtifact(
                    path=artifact.path, kind=artifact.type,
                    description=artifact.description, exists=raw is not None,
                    content=content, source="github",
                )]
            return [CollectedArtifact(
                path=artifact.path, kind=artifact.type,
                description=artifact.description, exists=True, source="github",
            )]

        text = self._fetch(artifact.path)
        if text is None:
            return [CollectedArtifact(
                path=artifact.path, kind=artifact.type,
                description=artifact.description, exists=False, source="github",
            )]

        markdown_cells: list[str] = []
        csv_rows: list[list[str]] = []
        content: str | None = None
        image_artifacts: list[CollectedArtifact] = []

        if artifact.type == "notebook" and suffix == "ipynb":
            try:
                stem = artifact.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                prefix = re.sub(r"[^0-9A-Za-z]+", "_", stem) + "_"
                content, cell_images = serialize_notebook(text, image_prefix=prefix)
                markdown_cells = notebook_markdown_from_content(text)
                for name, img_bytes in cell_images:
                    # path == bare filename so run.py saves it into github_images/<name>,
                    # matching the [output image: github_images/<name>] marker in the text.
                    image_artifacts.append(CollectedArtifact(
                        path=name, kind="figure", source="github", exists=True,
                        description=f"cell output image from {artifact.path}",
                        raw_bytes=img_bytes,
                    ))
            except Exception as exc:
                print(f"[GitHub] Warning: could not parse notebook {artifact.path!r}: {exc}", flush=True)
                content = text  # fall back to raw JSON so Claude still has the content
                markdown_cells = []
        elif suffix in ("csv", "tsv"):
            csv_rows = csv_rows_from_text(text)
            content = csv_table_from_rows(csv_rows)
        elif f".{suffix}" in TEXT_SUFFIXES or suffix in ("py", "r", "jl", "m", "sh"):
            content = text

        notebook_artifact = CollectedArtifact(
            path=artifact.path, kind=artifact.type,
            description=artifact.description, exists=True,
            content=content, markdown_cells=markdown_cells,
            csv_rows=csv_rows, source="github",
        )
        return [notebook_artifact, *image_artifacts]

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

        # Step 4 — collect all artifact content. Prefer one tarball request for
        # the whole folder; if that fails, fetch per-file concurrently.
        # pool.map preserves artifact order either way.
        self._load_tarball()
        artifacts = list(iter_all_artifacts(provisional_config))
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(artifacts)))) as pool:
            # _collect returns a list (notebook + its output images); flatten.
            collected = [a for sub in pool.map(self._collect, artifacts) for a in sub]

        # Step 5 — re-rank primary notebook by code cell count. Count real code-cell
        # headers in the serialized content (robust to '[code]' appearing in output text).
        def _code_cells(path: str) -> int:
            art = next((a for a in collected if a.path == path and a.kind == "notebook"), None)
            return len(re.findall(r"(?m)^## Cell \d+ \[code\]", art.content or "")) if art else 0

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

        # Provenance — resolve the commit once and stamp every artifact (WS2).
        self._resolve_commit()
        if self.commit_sha:
            ref_str = f"{self.owner}/{self.repo}@{self.commit_sha[:7]}"
            for a in collected:
                a.source_ref = ref_str
                a.updated_at = self.commit_date

        return ExperimentBundle(
            root_dir=self._url,
            config=config,
            collected_artifacts=collected,
        )
