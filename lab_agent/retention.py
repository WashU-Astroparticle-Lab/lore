"""
What LORE is allowed to delete, and when.

Measured on a real tree (2026-09-06): `outputs/` was 243.5 MB, of which the
*record* — every report, extraction, critique, provenance table and metadata
file across nine runs — was **1.0 MB**. The other 242.5 MB was figures, and
167 MB of that was byte-identical copies of the same LabArchives images fetched
into four different run directories.

So the policy is not "delete old things". It is:

  RECORD          never auto-deleted. Reports, extractions, connections,
                  critiques, provenance, metadata, the crawled corpus, the
                  knowledge bundle. A megabyte. There is no reason to touch it.

  RECONSTRUCTIBLE deletable, because it can be fetched again. Figures under
                  outputs/<run>/{github_images,labarchives_images}/ and
                  knowledge/image_cache/. GitHub figures need only the commit
                  SHA already pinned in metadata.json; LabArchives figures need
                  a live cookie.

  EPHEMERAL       deleted aggressively. Slack attachment downloads (OS temp,
                  one day), session logs beyond a keep-count.

The trigger for reclaiming a run's figures is a *lifecycle event*, not a timer:
once the report has been uploaded, LabArchives holds it and the local figures are
a working copy. `metadata.json.labarchives_upload` records that. Runs predating
that field fall back to "a report exists and the run is older than N days", and
that inference is reported as an inference.

Stripping writes `images_pruned.json` so the removal is legible and reversible,
and so `eval check` can downgrade "embedded image not found" from ERROR to WARN
for a run that was deliberately slimmed.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_DIRS = ("github_images", "labarchives_images")
PRUNE_MANIFEST = "images_pruned.json"

# A run must be at least this old before its figures can be reclaimed on the
# inferred (pre-labarchives_upload) path.
LEGACY_MIN_AGE_DAYS = 30


def _size(paths) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def run_images(run_dir: Path) -> list[Path]:
    """Figure files belonging to a run (only inside the known image dirs)."""
    out: list[Path] = []
    for name in IMAGE_DIRS:
        d = run_dir / name
        if d.is_dir():
            out += [f for f in d.rglob("*") if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES]
    return out


def classify_run(run_dir: Path, legacy_min_age_days: int = LEGACY_MIN_AGE_DAYS) -> dict:
    """Describe one run: sizes, whether it is uploaded, and what is reclaimable."""
    images = run_images(run_dir)
    text = [f for f in run_dir.rglob("*")
            if f.is_file() and f.suffix.lower() not in IMAGE_SUFFIXES]
    meta_path = run_dir / "metadata.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    uploaded = bool(meta.get("labarchives_upload"))
    has_report = any(f.name.startswith("[UNSIGNED]") and f.suffix == ".md"
                     for f in run_dir.glob("*.md"))
    age_days = (time.time() - run_dir.stat().st_mtime) / 86400
    already_pruned = (run_dir / PRUNE_MANIFEST).exists()

    if already_pruned and not images:
        reason = "already slimmed"
        reclaimable = False
    elif uploaded:
        reason = "uploaded — LabArchives holds the report"
        reclaimable = True
    elif has_report and age_days >= legacy_min_age_days:
        reason = (f"report present, {age_days:.0f}d old — upload INFERRED "
                  "(predates upload tracking)")
        reclaimable = True
    elif not has_report:
        reason = "no report yet — this run is still in progress"
        reclaimable = False
    else:
        reason = f"report present but only {age_days:.0f}d old — too recent to infer upload"
        reclaimable = False

    return {
        "dir": run_dir,
        "images_bytes": _size(images),
        "text_bytes": _size(text),
        "image_count": len(images),
        "uploaded": uploaded,
        "has_report": has_report,
        "age_days": age_days,
        "reclaimable": reclaimable,
        "reason": reason,
        "already_pruned": already_pruned,
    }


def strip_images(run_dir: Path, apply: bool = False) -> tuple[int, int]:
    """Remove a run's figures, leaving a manifest. Returns (files, bytes).

    The report text, extractions and metadata are untouched — they are the record
    and they are tiny. `metadata.json` keeps the commit SHA, so GitHub figures can
    be re-fetched exactly; LabArchives figures need a cookie.
    """
    images = run_images(run_dir)
    if not images:
        return 0, 0
    freed = _size(images)
    manifest = {
        "pruned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reason": "figures are reconstructible; the report is in LabArchives",
        "files": sorted(str(f.relative_to(run_dir)).replace("\\", "/") for f in images),
        "bytes_freed": freed,
        "regenerate": (
            "GitHub figures: re-run run.py with the github_url + commit SHA in "
            "metadata.json. LabArchives figures: python -m lab_agent.cli.fetch_page_images "
            "\"<page>\" (needs a live LA_SESSION_COOKIE)."
        ),
    }
    if not apply:
        return len(images), freed
    for f in images:
        try:
            f.unlink()
        except OSError:
            continue
    (run_dir / PRUNE_MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(images), freed


def duplicate_stats(root: Path) -> dict:
    """Byte-identical figures repeated across runs — waste with no information loss.

    The same LabArchives page fetched by four runs yields four copies of every
    figure on it. This is the single largest consumer measured, ahead of staleness.
    """
    by_hash: dict[str, list[Path]] = defaultdict(list)
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES:
            try:
                by_hash[hashlib.md5(f.read_bytes()).hexdigest()].append(f)
            except OSError:
                continue
    dupes = {h: ps for h, ps in by_hash.items() if len(ps) > 1}
    wasted = sum(ps[0].stat().st_size * (len(ps) - 1) for ps in dupes.values())
    worst = sorted(dupes.values(), key=lambda ps: -ps[0].stat().st_size * (len(ps) - 1))[:5]
    return {
        "unique": len(by_hash),
        "duplicated_hashes": len(dupes),
        "wasted_bytes": wasted,
        "worst": [
            {"name": ps[0].name,
             "copies": len(ps),
             "bytes_each": ps[0].stat().st_size,
             "runs": sorted({p.relative_to(root).parts[0] for p in ps})}
            for ps in worst
        ],
    }
