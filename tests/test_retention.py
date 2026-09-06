"""Retention policy — what may be deleted, and on what signal.

Measured on the real tree: outputs/ was 243.5 MB, of which the record (reports,
extractions, critiques, provenance, metadata across nine runs) was 1.0 MB. The
rest was figures, and 167 MB of that was byte-identical copies of the same
LabArchives images fetched into four run directories.

So the tests here are about the *trigger*: figures go once a run's report is in
LabArchives, never while a run is still in progress, and never the text.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.retention import (
    PRUNE_MANIFEST, classify_run, duplicate_stats, idle_days, in_active_use,
    run_images, strip_images, touch_used,
)

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


def _run(root: Path, name: str, *, report: bool, uploaded: bool,
         age_days: float = 0, n_images: int = 3, img_bytes: int = 1000) -> Path:
    d = root / name
    (d / "github_images").mkdir(parents=True)
    for i in range(n_images):
        (d / "github_images" / f"fig_{i}.png").write_bytes(b"p" * img_bytes)
    (d / "extracted_github.md").write_text("## Key Parameters\n", encoding="utf-8")
    (d / "connections.md").write_text("## Power chain closure\n", encoding="utf-8")
    meta = {"experiment_id": name, "github_commit_sha": "abc123"}
    if uploaded:
        meta["labarchives_upload"] = {"page_title": f"[UNSIGNED] {name}", "folder": "AI Agent"}
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    if report:
        (d / f"[UNSIGNED] {name}.md").write_text("# report\n", encoding="utf-8")
    when = time.time() - age_days * 86400
    os.utime(d, (when, when))
    return d


def _entry(root: Path, name: str, size_bytes: int, age_days: float) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "fig.png").write_bytes(b"x" * size_bytes)
    when = time.time() - age_days * 86400
    os.utime(d / "fig.png", (when, when))
    os.utime(d, (when, when))
    return d


def test_trigger_is_upload_not_age() -> None:
    root = Path(tempfile.mkdtemp())
    # Upload alone is no longer sufficient: the run must ALSO be idle. A run
    # written yesterday is likely still under discussion, and deleting its
    # figures would make the next question slow.
    fresh = _run(root, "uploaded_yesterday", report=True, uploaded=True, age_days=1)
    check("a freshly written uploaded run is protected",
          classify_run(fresh)["reclaimable"] is False)
    check("and the reason is active use", "IN USE" in classify_run(fresh)["reason"])

    up = _run(root, "uploaded_and_idle", report=True, uploaded=True, age_days=30)
    check("an uploaded run is reclaimable once idle",
          classify_run(up)["reclaimable"] is True)
    check("the reason names the upload", "uploaded" in classify_run(up)["reason"])

    wip = _run(root, "in_progress", report=False, uploaded=False, age_days=99)
    check("a run with no report is never reclaimed, however old",
          classify_run(wip)["reclaimable"] is False)
    check("the reason says it is in progress", "in progress" in classify_run(wip)["reason"])

    recent = _run(root, "recent_unuploaded", report=True, uploaded=False, age_days=5)
    check("a recent un-uploaded run is kept", classify_run(recent)["reclaimable"] is False)
    check("recency is reported, not silently applied",
          any(w in classify_run(recent)["reason"] for w in ("IN USE", "too recent")))

    legacy = _run(root, "legacy", report=True, uploaded=False, age_days=99)
    info = classify_run(legacy)
    check("an old run with a report is reclaimed by inference", info["reclaimable"] is True)
    check("and the inference is labelled as one", "INFERRED" in info["reason"])


def test_strip_keeps_the_record() -> None:
    root = Path(tempfile.mkdtemp())
    d = _run(root, "exp", report=True, uploaded=True, n_images=4, img_bytes=2000)

    n, freed = strip_images(d, apply=False)
    check("dry run reports without deleting", n == 4 and len(run_images(d)) == 4)

    n, freed = strip_images(d, apply=True)
    check("figures are removed", n == 4 and freed >= 8000 and run_images(d) == [])
    check("the report survives", (d / "[UNSIGNED] exp.md").exists())
    check("the extractions survive", (d / "extracted_github.md").exists())
    check("connections survives", (d / "connections.md").exists())
    check("metadata survives", (d / "metadata.json").exists())

    manifest = json.loads((d / PRUNE_MANIFEST).read_text(encoding="utf-8"))
    check("the manifest lists what went", len(manifest["files"]) == 4)
    check("the manifest says how to regenerate", "fetch_page_images" in manifest["regenerate"])
    check("the commit SHA is still there to re-fetch with",
          json.loads((d / "metadata.json").read_text())["github_commit_sha"] == "abc123")

    check("a slimmed run is not reclaimed twice", classify_run(d)["reclaimable"] is False)


def test_active_use_beats_every_other_signal() -> None:
    """A figure under discussion must survive, even if the run is uploaded.

    "Uploaded" says the report is safe in LabArchives. It says nothing about
    whether someone is mid-conversation about a figure in that run — and
    re-fetching one costs a live cookie and possibly a Duo tap, which is exactly
    the latency this pipeline exists to remove.
    """
    root = Path(tempfile.mkdtemp())
    d = _run(root, "uploaded_but_busy", report=True, uploaded=True, age_days=200)
    check("an old uploaded run is reclaimable when idle", classify_run(d)["reclaimable"] is True)

    touch_used(d)
    info = classify_run(d)
    check("marking it used makes it unreclaimable", info["reclaimable"] is False)
    check("the reason says it is in use", "IN USE" in info["reason"])
    check("idle time reads as ~zero", idle_days(d) < 0.01)

    # Passing a file marks its containing directory — callers hold figure paths.
    d2 = _run(root, "touched_via_file", report=True, uploaded=True, age_days=200)
    touch_used(d2 / "github_images" / "fig_0.png")
    check("touching a file marks its directory",
          in_active_use(d2 / "github_images") is True)

    check("a never-touched dir falls back to mtime", idle_days(_run(
        root, "untouched", report=True, uploaded=True, age_days=50)) > 49)

    check("touch_used never raises on a bad path",
          touch_used(root / "does" / "not" / "exist") is None)


def test_cache_prune_respects_active_use() -> None:
    """The image cache is the conversation-latency path — recency of USE, not creation."""
    from lab_agent.cache_prune import plan_prune

    root = Path(tempfile.mkdtemp())
    old_busy = _entry(root, "page_being_discussed", 1000, age_days=99)
    old_idle = _entry(root, "page_archived", 1000, age_days=99)
    touch_used(old_busy)

    doomed = {p.name for p, _s, _r in plan_prune(root, max_age_days=14, max_mb=999)}
    check("an old but in-use page is kept", "page_being_discussed" not in doomed)
    check("an old idle page still goes", "page_archived" in doomed)


def test_duplicate_detection() -> None:
    root = Path(tempfile.mkdtemp())
    same = b"x" * 5000
    for name in ("run_a", "run_b", "run_c"):
        d = root / name / "labarchives_images"
        d.mkdir(parents=True)
        (d / "shared.png").write_bytes(same)
    (root / "run_a" / "labarchives_images" / "unique.png").write_bytes(b"y" * 1000)

    stats = duplicate_stats(root)
    check("duplicate content is detected", stats["duplicated_hashes"] == 1)
    check("wasted space counts only the extra copies", stats["wasted_bytes"] == 2 * 5000)
    check("unique files are counted separately", stats["unique"] == 2)
    check("the worst offender names every run holding it",
          set(stats["worst"][0]["runs"]) == {"run_a", "run_b", "run_c"})


def test_gate_tolerates_a_slimmed_run() -> None:
    """eval check must not ERROR on a figure that cleanup deliberately removed."""
    from lab_agent.cli import eval as ev

    root = Path(tempfile.mkdtemp())
    (root / "github_images").mkdir()
    (root / "[UNSIGNED] exp.md").write_text(
        "# [UNSIGNED] exp\n\n**Experiment:** exp\n**Report generated:** 2026-09-06\n\n"
        "## Main Result: it worked\n\n![a plot](github_images/gone.png)\n",
        encoding="utf-8")
    (root / "slack_summary.md").write_text("- one finding\n", encoding="utf-8")
    (root / "provenance.md").write_text(
        "| a | b | c | d |\n|---|---|---|---|\n| 1 | x | y | z |\n", encoding="utf-8")

    errors = [m for lvl, m in ev.check(root) if lvl == "ERROR"]
    check("a genuinely missing image is still an ERROR",
          any("image not found" in m for m in errors))

    (root / PRUNE_MANIFEST).write_text('{"files": ["github_images/gone.png"]}', encoding="utf-8")
    findings = ev.check(root)
    errors = [m for lvl, m in findings if lvl == "ERROR"]
    warns = [m for lvl, m in findings if lvl == "WARN"]
    check("after slimming it is no longer an ERROR",
          not any("image not found" in m for m in errors))
    check("it is reported as a WARN instead", any("slimmed" in m for m in warns))


if __name__ == "__main__":
    test_trigger_is_upload_not_age()
    test_strip_keeps_the_record()
    test_active_use_beats_every_other_signal()
    test_cache_prune_respects_active_use()
    test_duplicate_detection()
    test_gate_tolerates_a_slimmed_run()
    print(f"\ntest_retention: {PASSED} checks passed")
