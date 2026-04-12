"""
run.py — pipeline runner.

Usage:
    python run.py "<github_url>" "<la_page_1>" "<la_page_2>" ...

Fetches all experiment artifacts from GitHub and LabArchives and saves
the content to the output folder. Claude then reads those files and
writes experiment_report.md directly.

Required env vars (set in .env):
    GITHUB_TOKEN  — GitHub personal access token (needed for private repos)
    LA_AKID       — LabArchives access key ID
    LA_SECRET     — LabArchives institutional API password (full password)
    LA_UID        — LabArchives numeric user ID

Output folder: outputs/<experiment_id>/
    notebooks.md         — all notebook content
    labarchives.md       — lab notebook notes from LabArchives
    data_summaries.md    — CSV file contents and numeric ranges
    experiment_report.md — written by Claude after reading the above
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from lab_agent.models import ExperimentBundle
from lab_agent.sources import GitHubAdapter, LabArchivesAdapter
from lab_agent.sources.github import parse_github_url
from lab_agent.summarize import summarize_bundle
from lab_agent.dependencies import resolve_dependencies

OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs")


def run(github_url: str, la_pages: list[str]) -> str:
    # 1. Fetch GitHub artifacts
    print(f"[runner] Fetching GitHub: {github_url}")
    bundle = GitHubAdapter(github_url).load()

    # 2. Fetch LabArchives pages
    if la_pages:
        la_artifacts = []
        for page in la_pages:
            print(f"[runner] Fetching LabArchives: {page!r}")
            la_artifacts.extend(LabArchivesAdapter(page).fetch())
        bundle = ExperimentBundle(
            root_dir=bundle.root_dir,
            config=bundle.config,
            collected_artifacts=bundle.collected_artifacts + la_artifacts,
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

    # 8. Resolve lab-specific imports and fetch their source from GitHub
    owner, _, _, _ = parse_github_url(github_url)
    resolve_dependencies(out_dir, bundle.collected_artifacts, owner)

    print(f"[runner] Output folder: {out_dir}")
    print(f"[runner] Artifacts: {', '.join(e.rsplit(' (', 1)[0] for e in summary.evidence_map)}")
    return str(out_dir)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    run(sys.argv[1], sys.argv[2:])
