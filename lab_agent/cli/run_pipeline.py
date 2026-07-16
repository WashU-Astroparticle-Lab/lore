"""
Experiment pipeline runner (invoked via the root-level run.py shim).

Usage:
    python run.py "<github_url>" "<la_page_1>" "<la_page_2>" ...   # full pipeline
    python run.py "<la_page_1>" "<la_page_2>" ...                   # LabArchives-only

The GitHub URL is optional. If omitted, the pipeline fetches LabArchives pages first
and then scans their content for embedded GitHub links — if any are found, they are
fetched automatically. If none are found, the pipeline runs in LabArchives-only mode.

Fetches all experiment artifacts and saves the content to the output folder.
Claude then reads those files and writes the report.

Required env vars (set in .env):
    GITHUB_TOKEN  — GitHub personal access token (needed for private repos)
    LA_AKID       — LabArchives access key ID
    LA_SECRET     — LabArchives institutional API password (full password)
    LA_UID        — LabArchives numeric user ID

Output folder: outputs/<experiment_id>/
    notebooks.md         — all notebook content (if GitHub was available)
    labarchives.md       — lab notebook notes from LabArchives
    data_summaries.md    — CSV file contents and numeric ranges
    dependencies.md      — source code of lab-specific imports (if GitHub available)
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import OUTPUT_ROOT, load_env

load_env()

from ..collect.dependencies import resolve_dependencies
from ..collect.summarize import summarize_bundle
from ..models import (
    ArtifactGroup, CollectedArtifact, ExperimentBundle,
    ExperimentConfig, ExperimentMeta, Objective,
)
from ..sources import GitHubAdapter, LabArchivesAdapter
from ..sources.github import parse_github_url

_ID_STOPWORDS = {"and", "or", "the", "a", "an", "in", "to", "of", "for", "with",
                 "new", "at", "by", "on", "its", "is", "was", "are"}


def _la_pages_to_experiment_id(la_pages: list[str]) -> str:
    """Derive a filesystem-safe experiment ID from LabArchives page names.

    Format: YYYYMMDD_word1_word2_word3 — date first (for sorting), then the
    first three meaningful words from the page title.
    """
    first = la_pages[0] if la_pages else "labarchives_run"
    date_m = re.search(r"\d{8}", first)
    date_str = date_m.group() if date_m else ""

    without_date = re.sub(r"\d{8}", "", first)
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", without_date)
    meaningful = [w.lower() for w in words
                  if len(w) > 2 and w.lower() not in _ID_STOPWORDS][:3]
    slug = "_".join(meaningful) if meaningful else "run"

    return f"{date_str}_{slug}" if date_str else slug


_GITHUB_URL_RE = re.compile(r"https?://github\.com/[^\s\)\"'<>]+")
_BLOB_RE = re.compile(r"(https://github\.com/[^/]+/[^/]+)/blob/([^/]+)/(.*)")
_REPO_ROOT_RE = re.compile(r"https?://github\.com/[^/]+/[^/?#]+/?$")


def _find_github_urls(artifacts: list[CollectedArtifact]) -> list[str]:
    """Scan artifact text content for GitHub URLs.

    Normalizes /blob/ref/path/file links to /tree/ref/parent_dir so
    GitHubAdapter can use them directly. Only returns URLs with /tree/ paths.
    Bare repo root URLs are normalized to /tree/main.
    """
    seen: set[str] = set()
    urls: list[str] = []
    for art in artifacts:
        if not art.content:
            continue
        for m in _GITHUB_URL_RE.finditer(art.content):
            url = m.group().rstrip(".,;)")
            blob_m = _BLOB_RE.match(url)
            if blob_m:
                base, ref, path = blob_m.group(1), blob_m.group(2), blob_m.group(3)
                # Trim to parent directory if path ends with a filename (has extension)
                last_seg = path.rstrip("/").split("/")[-1]
                if "." in last_seg:
                    path = "/".join(path.rstrip("/").split("/")[:-1])
                url = f"{base}/tree/{ref}/{path}".rstrip("/")
            elif _REPO_ROOT_RE.match(url):
                url = url.rstrip("/") + "/tree/main"

            if "/tree/" in url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def run(github_url: str | None, la_pages: list[str]) -> str:
    la_artifacts: list[CollectedArtifact] = []

    # 1. Fetch LabArchives pages first — they may contain GitHub URLs.
    # Pages are independent, so fetch them concurrently; pool.map preserves
    # input order so the assembled artifacts match the sequential layout.
    if la_pages:
        for page in la_pages:
            print(f"[runner] Fetching LabArchives: {page!r}")
        with ThreadPoolExecutor(max_workers=min(4, len(la_pages))) as pool:
            for arts in pool.map(lambda p: LabArchivesAdapter(p).fetch(), la_pages):
                la_artifacts.extend(arts)

    # If no GitHub URL was given, look for one embedded in the LabArchives content
    discovered_urls: list[str] = []
    if not github_url:
        discovered_urls = _find_github_urls(la_artifacts)
        if discovered_urls:
            github_url = discovered_urls[0]
            print(f"[runner] Discovered GitHub URL in LabArchives content: {github_url}")
            if len(discovered_urls) > 1:
                print(f"[runner] Additional GitHub URLs found (not fetched): {discovered_urls[1:]}")

    # 2. Fetch GitHub artifacts (if a URL is available)
    github_bundle = None
    if github_url:
        print(f"[runner] Fetching GitHub: {github_url}")
        try:
            github_bundle = GitHubAdapter(github_url).load()
        except Exception as exc:
            if discovered_urls and github_url in discovered_urls:
                # Auto-discovered URL failed — warn and continue LabArchives-only
                print(f"[runner] Warning: auto-discovered GitHub URL failed ({exc}). Continuing LabArchives-only.")
                github_url = None
            else:
                raise

    if github_bundle is not None:
        config = github_bundle.config
        # When the GitHub URL was auto-discovered from LabArchives content (not explicitly
        # given by the user), the folder name may be a generic subfolder like "notebooks".
        # Override the experiment ID with one derived from the LabArchives page names so
        # the report gets a meaningful name.
        if discovered_urls and la_pages:
            la_id = _la_pages_to_experiment_id(la_pages)
            config = config.model_copy(
                update={"experiment": config.experiment.model_copy(update={"id": la_id})}
            )
            print(f"[runner] Overriding experiment_id to LabArchives-derived: {la_id!r}")
        bundle = ExperimentBundle(
            root_dir=github_bundle.root_dir,
            config=config,
            collected_artifacts=github_bundle.collected_artifacts + la_artifacts,
        )
    else:
        # LabArchives-only mode — build a minimal bundle
        experiment_id = _la_pages_to_experiment_id(la_pages)
        print(f"[runner] LabArchives-only mode. experiment_id={experiment_id!r}")
        bundle = ExperimentBundle(
            root_dir=str(OUTPUT_ROOT),
            config=ExperimentConfig(
                experiment=ExperimentMeta(
                    id=experiment_id,
                    title=experiment_id.replace("_", " ").title(),
                ),
                objective=Objective(research_question="See LabArchives notes."),
                artifacts=ArtifactGroup(),
            ),
            collected_artifacts=la_artifacts,
        )

    # 3. Extract structured content
    summary = summarize_bundle(bundle)
    out_dir = Path(OUTPUT_ROOT) / summary.output_dirname
    out_dir.mkdir(parents=True, exist_ok=True)

    # 4. Save notebook content
    if summary.notebook_markdown:
        nb_text = "\n\n---\n\n".join(summary.notebook_markdown)
        (out_dir / "notebooks.md").write_text(nb_text, encoding="utf-8")
        print(f"[runner] Saved notebooks.md")

    # 5. Save LabArchives notes
    if summary.labarchives_context:
        la_text = "\n\n---\n\n".join(summary.labarchives_context)
        (out_dir / "labarchives.md").write_text(la_text, encoding="utf-8")
        print(f"[runner] Saved labarchives.md")

    # 5b. Save LabArchives image attachments and write a manifest for Claude to read
    la_images = [
        art for art in bundle.collected_artifacts
        if art.source == "labarchives" and art.kind == "figure" and art.raw_bytes
    ]
    if la_images:
        images_dir = out_dir / "labarchives_images"
        images_dir.mkdir(exist_ok=True)
        manifest_lines = ["# LabArchives Images", "", "Read each path below with the Read tool.", ""]
        for art in la_images:
            filename = art.path.rsplit("/", 1)[-1]
            img_path = images_dir / filename
            img_path.write_bytes(art.raw_bytes)
            print(f"[runner] Saved image: {img_path}")
            manifest_lines.append(f"- `{img_path}` — {art.description or filename}")
        (out_dir / "labarchives_images.md").write_text(
            "\n".join(manifest_lines), encoding="utf-8"
        )
        print(f"[runner] Saved labarchives_images.md")

    # 6. Save GitHub image files
    gh_images = [
        art for art in bundle.collected_artifacts
        if art.source == "github" and art.raw_bytes
    ]
    if gh_images:
        images_dir = out_dir / "github_images"
        images_dir.mkdir(exist_ok=True)
        manifest_lines = ["# GitHub Images", "", "Read each path below with the Read tool.", ""]
        for art in gh_images:
            filename = art.path.rsplit("/", 1)[-1]
            img_path = images_dir / filename
            img_path.write_bytes(art.raw_bytes)
            print(f"[runner] Saved GitHub image: {img_path}")
            manifest_lines.append(f"- `{img_path}` — {art.description or filename}")
        (out_dir / "github_images.md").write_text(
            "\n".join(manifest_lines), encoding="utf-8"
        )
        print(f"[runner] Saved github_images.md")

    # 7. Save data file summaries
    data_parts = []
    if summary.csv_summaries:
        data_parts.extend(summary.csv_summaries)
    if summary.csv_tables:
        data_parts.extend(summary.csv_tables)
    if data_parts:
        (out_dir / "data_summaries.md").write_text("\n\n".join(data_parts), encoding="utf-8")
        print(f"[runner] Saved data_summaries.md")

    # 8. Resolve lab-specific imports (only if GitHub is available)
    if github_url:
        owner, _, _, _ = parse_github_url(github_url)
        resolve_dependencies(out_dir, bundle.collected_artifacts, owner)

    # 9. Write metadata for downstream phase scripts
    metadata = {
        "experiment_id": summary.output_dirname,
        "la_pages": la_pages,
        "github_url": github_url or "",
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[runner] Saved metadata.json")

    print(f"[runner] Output folder: {out_dir}")
    print(f"[runner] Artifacts: {', '.join(e.rsplit(' (', 1)[0] for e in summary.evidence_map)}")
    return str(out_dir)


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    # First arg is a GitHub URL if it starts with http and contains github.com
    if args[0].startswith("http") and "github.com" in args[0]:
        run(args[0], args[1:])
    else:
        run(None, args)


if __name__ == "__main__":
    main()
