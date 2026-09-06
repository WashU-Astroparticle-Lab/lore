"""
Experiment pipeline runner (invoked via the root-level run.py shim).

Usage:
    python run.py "<github_url>" "<la_page_1>" "<la_page_2>" ...   # full pipeline
    python run.py "<la_page_1>" "<la_page_2>" ...                   # LabArchives-only

Options:
    --experiment-id NAME  write to outputs/NAME/ instead of the derived id. Use this
                          when a run's code lives in another run's GitHub folder, so
                          it gets its own directory instead of overwriting that run.
    --out-dir PATH        explicit output directory (overrides --experiment-id)
    --reuse               write into an existing directory that holds different inputs
    --force               skip the collision check entirely

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
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from ..config import OUTPUT_ROOT, PROJECT_ROOT, lab_config_value, load_env

load_env()

from ..collect import okf
from ..collect.dependencies import build_dependencies_md
from ..collect.summarize import summarize_bundle
from ..models import (
    ArtifactGroup, CollectedArtifact, ExperimentBundle,
    ExperimentConfig, ExperimentMeta, Objective,
)
from ..sources import GitHubAdapter, LabArchivesAdapter
from ..sources.github import parse_github_url
from ..sources.labarchives.auth import cookies_still_valid

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

# Auto-discovery fetches a URL found inside untrusted LabArchives content, so it
# is gated in code (not left to the prompt): the host must be GitHub, an optional
# org allowlist can restrict the owner, and the number of discovered URLs is capped.
_ALLOWED_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_MAX_DISCOVERED_URLS = 10


def _allowed_github_orgs() -> set[str]:
    """Optional owner allowlist from lab_config.md ('GitHub orgs', comma-separated).

    Empty (key absent) means no org restriction — any github.com owner is allowed.
    """
    raw = lab_config_value("GitHub orgs")
    return {o.strip().lower() for o in raw.split(",") if o.strip()}


def _github_url_allowed(url: str, allowed_orgs: set[str]) -> bool:
    """Host- and owner-allowlist gate for an auto-discovered GitHub URL."""
    parts = urlparse(url)
    if parts.netloc.lower() not in _ALLOWED_GITHUB_HOSTS:
        return False
    if allowed_orgs:
        owner = parts.path.lstrip("/").split("/", 1)[0].lower()
        if owner not in allowed_orgs:
            return False
    return True


def _find_github_urls(artifacts: list[CollectedArtifact]) -> list[str]:
    """Scan artifact text content for GitHub URLs.

    Normalizes /blob/ref/path/file links to /tree/ref/parent_dir so
    GitHubAdapter can use them directly. Only returns URLs with /tree/ paths.
    Bare repo root URLs are normalized to /tree/main. Results are gated by an
    allowlist (host, optional org) and capped at _MAX_DISCOVERED_URLS.
    """
    allowed_orgs = _allowed_github_orgs()
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
                if not _github_url_allowed(url, allowed_orgs):
                    print(f"[runner] Skipping discovered GitHub URL (host/org not allowed): {url}")
                    continue
                seen.add(url)
                urls.append(url)
                if len(urls) >= _MAX_DISCOVERED_URLS:
                    print(f"[runner] Discovered-URL cap ({_MAX_DISCOVERED_URLS}) reached; "
                          "ignoring further links.")
                    return urls
    return urls


_WIRING_CACHE = PROJECT_ROOT / "cache" / "wiring_diagram.md"
_WIRING_TTL_SECONDS = 7 * 24 * 3600


def _fetch_wiring_diagram() -> str | None:
    """Return the wiring diagram page as Markdown, from cache when fresh.

    The labarchives-analyst agent needs this page every run; prefetching it
    here (text only, concurrently with the page fetches) saves the agent a
    live LabArchives search at report time. The diagram rarely changes, so a
    7-day disk cache skips even that.
    """
    title = lab_config_value("Wiring diagram page").strip()
    if not title:
        return None
    if _WIRING_CACHE.exists() and time.time() - _WIRING_CACHE.stat().st_mtime < _WIRING_TTL_SECONDS:
        print("[runner] Wiring diagram served from cache.")
        return _WIRING_CACHE.read_text(encoding="utf-8")
    try:
        arts = LabArchivesAdapter(title).fetch(include_images=False)
    except Exception as exc:
        print(f"[runner] Warning: wiring diagram fetch failed ({exc}); "
              "the labarchives-analyst will fetch it live instead.")
        return None
    text = "\n\n".join(a.content for a in arts if a.content)
    if not text:
        return None
    md = f"# Wiring Diagram — {title}\n\n{text}"
    try:
        _WIRING_CACHE.parent.mkdir(exist_ok=True)
        _WIRING_CACHE.write_text(md, encoding="utf-8")
    except OSError as exc:
        print(f"[runner] Warning: could not cache wiring diagram: {exc}")
    return md


_re_gh = re.compile(
    r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/(?:tree|blob)/[^/]+(/.*)?)?/?$"
)


def _gh_identity(url: str | None) -> str:
    """Identity of a GitHub URL ignoring the ref, so re-fetching the same
    experiment at a newer commit is not mistaken for a different experiment.

    ``.../owner/repo/tree/<sha>/DAQ/foo`` -> ``owner/repo/DAQ/foo``
    """
    if not url:
        return ""
    m = _re_gh.search(url.strip())
    if not m:
        return url.strip()
    owner, repo, subpath = m.group(1), m.group(2), (m.group(3) or "").strip("/")
    return f"{owner}/{repo}/{subpath}".rstrip("/")


def _guard_out_dir(
    out_dir: Path,
    github_url: str | None,
    la_pages: list[str],
    *,
    reuse: bool = False,
    force: bool = False,
) -> None:
    """Refuse to write a *different* experiment into an existing output directory.

    The experiment id is derived from the GitHub folder name, so two experiments
    that share a code folder (e.g. a follow-up run whose notebook lives in the
    previous run's directory) silently collide and the second overwrites the
    first's metadata.json, destroying its provenance. Catch that here.
    """
    meta_path = out_dir / "metadata.json"
    if force or not meta_path.exists():
        return
    try:
        prev = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return  # unreadable metadata is not a reason to block a run

    prev_gh, now_gh = _gh_identity(prev.get("github_url")), _gh_identity(github_url)
    prev_la = sorted(p.strip() for p in (prev.get("la_pages") or []))
    now_la = sorted(p.strip() for p in la_pages)
    if prev_gh == now_gh and prev_la == now_la:
        return  # same experiment — a plain re-run, which is fine

    if reuse:
        print(f"[runner] --reuse: writing into existing {out_dir.name}/ despite different inputs.")
        return

    print(
        f"[runner] REFUSING to overwrite {out_dir.name}/ — it holds a different experiment.\n"
        f"  existing: github={prev_gh or '(none)'} la_pages={prev_la or '(none)'}\n"
        f"  this run: github={now_gh or '(none)'} la_pages={now_la or '(none)'}\n"
        "Writing here would overwrite that run's metadata.json and its provenance.\n"
        "Choose one:\n"
        "  --experiment-id <name>   write to a fresh outputs/<name>/ (recommended)\n"
        "  --reuse                  deliberately add to the existing directory\n"
        "  --force                  skip this check entirely"
    )
    sys.exit(4)


def run(
    github_url: str | None,
    la_pages: list[str],
    experiment_id: str | None = None,
    out_dir_override: Path | None = None,
    *,
    reuse: bool = False,
    force: bool = False,
) -> str:
    # Wall-clock stage timing (goal: keep the fetch layer "timely"). Marks are
    # consecutive; each stage duration is the gap between adjacent marks. The
    # summary is printed and stored in metadata.json under "fetch_timings_sec".
    _t_start = time.perf_counter()
    _marks: list[tuple[str, float]] = [("start", _t_start)]

    def _mark(label: str) -> None:
        _marks.append((label, time.perf_counter()))

    # 0. Fail fast on an expired web session BEFORE any fetching. Previously an
    # expired cookie only surfaced after the full text+image fetch, forcing a
    # complete rerun. One probe request catches it in seconds instead.
    cookie_str = os.environ.get("LA_SESSION_COOKIE", "").strip()
    if la_pages and cookie_str and not cookies_still_valid(cookie_str):
        print(
            "[runner] COOKIE_REFRESH_NEEDED: LA_SESSION_COOKIE is expired "
            "(detected by pre-fetch probe, nothing was fetched).\n"
            "Run:  python get_la_cookies.py\n"
            "Then rerun this command."
        )
        sys.exit(3)

    la_artifacts: list[CollectedArtifact] = []

    # Kick off the wiring diagram prefetch in the background; the result is
    # collected right before files are written.
    wiring_pool = ThreadPoolExecutor(max_workers=1)
    wiring_future = wiring_pool.submit(_fetch_wiring_diagram)
    wiring_pool.shutdown(wait=False)

    # 1. Fetch LabArchives pages first — they may contain GitHub URLs.
    # Pages are independent, so fetch them concurrently; pool.map preserves
    # input order so the assembled artifacts match the sequential layout.
    if la_pages:
        for page in la_pages:
            print(f"[runner] Fetching LabArchives: {page!r}")
        with ThreadPoolExecutor(max_workers=min(4, len(la_pages))) as pool:
            for arts in pool.map(lambda p: LabArchivesAdapter(p).fetch(), la_pages):
                la_artifacts.extend(arts)
    _mark("labarchives_fetch")

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
    gh_adapter = None
    if github_url:
        print(f"[runner] Fetching GitHub: {github_url}")
        try:
            gh_adapter = GitHubAdapter(github_url)
            github_bundle = gh_adapter.load()
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
        # Multi-repo (S2): when several GitHub URLs were auto-discovered in the
        # LabArchives content, fetch the extras too (capped, best-effort) so a
        # cross-repo experiment is complete instead of dropping all but the first.
        for extra_url in (discovered_urls[1:] if discovered_urls else [])[:3]:
            print(f"[runner] Fetching additional discovered repo: {extra_url}")
            try:
                bundle.collected_artifacts.extend(GitHubAdapter(extra_url).load().collected_artifacts)
            except Exception as exc:
                print(f"[runner] Warning: additional repo fetch failed ({exc}). Skipping.")
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

    _mark("github_fetch")

    # Kick off dependency resolution in the background (network-bound) so it
    # overlaps with summarization and file writing; joined at step 8.
    deps_future = None
    if github_url:
        owner, _, _, _ = parse_github_url(github_url)
        deps_pool = ThreadPoolExecutor(max_workers=1)
        deps_future = deps_pool.submit(build_dependencies_md, bundle.collected_artifacts, owner)
        deps_pool.shutdown(wait=False)

    # 3. Extract structured content
    summary = summarize_bundle(bundle)
    _mark("summarize")
    # The experiment id normally comes from the GitHub folder (or the LabArchives
    # pages in LA-only mode). An explicit --experiment-id/--out-dir wins, so a
    # follow-up run whose code lives in a previous run's folder can be given its
    # own directory instead of overwriting that run.
    dirname = experiment_id or summary.output_dirname
    out_dir = Path(out_dir_override) if out_dir_override else Path(OUTPUT_ROOT) / dirname
    if experiment_id or out_dir_override:
        print(f"[runner] Output directory overridden: {out_dir}")
    _guard_out_dir(out_dir, github_url, la_pages, reuse=reuse, force=force)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 4. Save notebook content — full serialized cells (code + outputs), one
    # section per notebook. Previously only markdown cells were written, so the
    # analysts never saw the code or the result plots (WS1). Title/objective
    # inference still uses markdown cells (handled in the GitHub adapter).
    notebook_arts = [
        a for a in bundle.collected_artifacts
        if a.kind == "notebook" and a.exists and a.content
    ]
    if notebook_arts:
        sections = [f"# {a.path}\n\n{a.content}" for a in notebook_arts]
        (out_dir / "notebooks.md").write_text("\n\n---\n\n".join(sections), encoding="utf-8")
        print(f"[runner] Saved notebooks.md")

    # 4b. Save standalone code scripts (.py/.r/.jl/.sh/.sql). Previously these were
    # fetched but never surfaced to the analysts (S2 ingestion completeness).
    script_arts = [
        a for a in bundle.collected_artifacts
        if a.kind == "code" and a.exists and a.content
    ]
    if script_arts:
        lang = {"py": "python", "r": "r", "jl": "julia", "sh": "bash", "sql": "sql"}
        sections = []
        for a in script_arts:
            suffix = a.path.rsplit(".", 1)[-1].lower() if "." in a.path else ""
            sections.append(f"# {a.path}\n\n```{lang.get(suffix, '')}\n{a.content}\n```")
        (out_dir / "scripts.md").write_text("\n\n---\n\n".join(sections), encoding="utf-8")
        print(f"[runner] Saved scripts.md")

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

    # 5c. Save the prefetched wiring diagram for the labarchives-analyst
    wiring_md = wiring_future.result()
    if wiring_md:
        (out_dir / "wiring_diagram.md").write_text(wiring_md, encoding="utf-8")
        print(f"[runner] Saved wiring_diagram.md")

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

    # 7. Save data file summaries (CSV tables/summaries + binary data files — S2)
    data_parts = []
    if summary.csv_summaries:
        data_parts.extend(summary.csv_summaries)
    if summary.csv_tables:
        data_parts.extend(summary.csv_tables)
    binary_summaries = [
        f"**{a.path}** — {a.content}"
        for a in bundle.collected_artifacts
        if a.kind == "processed_data" and a.exists and a.content
    ]
    data_parts.extend(binary_summaries)
    if data_parts:
        (out_dir / "data_summaries.md").write_text("\n\n".join(data_parts), encoding="utf-8")
        print(f"[runner] Saved data_summaries.md")

    # 8. Save lab-specific import sources (resolution started before step 3)
    if deps_future is not None:
        deps_md = deps_future.result()
        if deps_md:
            (out_dir / "dependencies.md").write_text(deps_md, encoding="utf-8")
            print(f"[deps] Saved dependencies.md ({len(deps_md):,} chars)")

    _mark("write_and_deps")

    # Per-stage wall-clock durations (seconds) from consecutive marks.
    timings = {
        label: round(t - _marks[i][1], 2)
        for i, (label, t) in enumerate(_marks[1:])
    }
    timings["total"] = round(_marks[-1][1] - _t_start, 2)

    # 9. Write metadata for downstream phase scripts (WS2 provenance enrichment)
    la_entry_count = sum(
        1 for a in bundle.collected_artifacts
        if a.source == "labarchives" and a.kind == "notes" and a.content
    )
    files_written = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    metadata = {
        "experiment_id": out_dir.name,
        "la_pages": la_pages,
        "github_url": github_url or "",
        "discovered_github_urls": discovered_urls,
        "github_commit_sha": gh_adapter.commit_sha if gh_adapter else None,
        "github_commit_date": gh_adapter.commit_date if gh_adapter else None,
        "github_tree_truncated": gh_adapter.tree_truncated if gh_adapter else False,
        "la_entry_count": la_entry_count,
        "run_timestamp": datetime.now().isoformat(timespec="seconds"),
        "files": files_written,
        "fetch_timings_sec": timings,
    }
    (out_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[runner] Saved metadata.json")

    # 10. OKF Phase 1 — make each collection file a self-describing concept and
    # write index.md, so the run directory is a browsable knowledge bundle.
    okf.finalize_bundle(
        out_dir,
        resource=github_url or "",
        source_ref=gh_adapter.commit_sha if gh_adapter else None,
        timestamp=gh_adapter.commit_date if gh_adapter else None,
        tags=[out_dir.name],
    )
    print(f"[runner] Wrote OKF frontmatter + index.md")

    print(f"[runner] Output folder: {out_dir}")
    print(f"[runner] Artifacts: {', '.join(e.rsplit(' (', 1)[0] for e in summary.evidence_map)}")
    print(f"[runner] Fetch timings (s): "
          + ", ".join(f"{k}={v}" for k, v in timings.items()))
    return str(out_dir)


def _parse_args(argv: list[str]) -> tuple[list[str], dict]:
    """Split argv into positionals and options.

    Options: --experiment-id NAME, --out-dir PATH, --reuse, --force
    (``--opt=value`` is accepted too).
    """
    opts: dict = {"experiment_id": None, "out_dir": None, "reuse": False, "force": False}
    positional: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        key, _, inline = arg.partition("=")
        if key in ("--experiment-id", "--out-dir"):
            field = "experiment_id" if key == "--experiment-id" else "out_dir"
            if inline:
                opts[field] = inline
                i += 1
            elif i + 1 < len(argv):
                opts[field] = argv[i + 1]
                i += 2
            else:
                print(f"[runner] {key} needs a value")
                sys.exit(1)
            continue
        if key in ("--reuse", "--force"):
            opts[key.lstrip("-")] = True
            i += 1
            continue
        positional.append(arg)
        i += 1
    return positional, opts


def main() -> None:
    positional, opts = _parse_args(sys.argv[1:])
    if not positional:
        print(__doc__)
        sys.exit(1)
    out_dir_override = Path(opts["out_dir"]) if opts["out_dir"] else None
    # First arg is a GitHub URL if it starts with http and contains github.com
    if positional[0].startswith("http") and "github.com" in positional[0]:
        github_url, la_pages = positional[0], positional[1:]
    else:
        github_url, la_pages = None, positional
    run(
        github_url,
        la_pages,
        experiment_id=opts["experiment_id"],
        out_dir_override=out_dir_override,
        reuse=opts["reuse"],
        force=opts["force"],
    )


if __name__ == "__main__":
    main()
