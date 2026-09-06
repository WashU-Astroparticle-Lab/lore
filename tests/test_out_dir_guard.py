"""Stage A2 — output-directory collision guard and CLI option parsing.

Regression target: on 2026-08-31 a follow-up run whose notebook lived in the
previous run's GitHub folder was written into outputs/presto_vna_spectrum_20260826/,
overwriting that experiment's metadata.json (it now cites the Aug 31 page and
commit). The guard must refuse that, while still allowing a plain re-run and a
re-fetch of the same experiment at a newer commit.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cli.run_pipeline import (
    _GENERIC_FOLDER_NAMES, _gh_identity, _guard_out_dir, _la_pages_to_experiment_id,
    _parse_args, _preflight_guard,
)

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


def _mkdir_with_meta(github_url: str, la_pages: list[str]) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "metadata.json").write_text(
        json.dumps({"github_url": github_url, "la_pages": la_pages}), encoding="utf-8"
    )
    return d


def test_gh_identity_ignores_ref() -> None:
    base = "https://github.com/WashU-Astroparticle-Lab/analysis_archive"
    a = f"{base}/tree/816564918222a5825a7f668e5bdc84fe9884ff9b/DAQ/presto_vna_spectrum_20260826"
    b = f"{base}/tree/b1e0501477ff2937e979051ff1c45197379b461e/DAQ/presto_vna_spectrum_20260826"
    check("same folder at different commits has one identity", _gh_identity(a) == _gh_identity(b))
    check(
        "identity is owner/repo/subpath",
        _gh_identity(a) == "WashU-Astroparticle-Lab/analysis_archive/DAQ/presto_vna_spectrum_20260826",
    )
    check("different subpath differs", _gh_identity(a) != _gh_identity(f"{base}/tree/main/DAQ/other"))
    check("empty url is empty identity", _gh_identity(None) == "" and _gh_identity("") == "")
    check("bare repo url does not crash", _gh_identity(base) == "WashU-Astroparticle-Lab/analysis_archive")


def test_guard_allows_plain_rerun() -> None:
    url = "https://github.com/o/r/tree/abc123/DAQ/exp"
    d = _mkdir_with_meta(url, ["page one"])
    _guard_out_dir(d, url, ["page one"])  # must not raise/exit
    check("identical inputs are allowed", True)

    newer = "https://github.com/o/r/tree/def456/DAQ/exp"
    _guard_out_dir(d, newer, ["page one"])
    check("re-fetch at a newer commit is allowed", True)

    _guard_out_dir(d, url, [" page one "])
    check("page whitespace is normalised", True)


def test_guard_blocks_the_real_collision() -> None:
    # Exactly the Aug 26 -> Aug 31 case: same GitHub folder, different LA pages.
    prev = "https://github.com/WashU-Astroparticle-Lab/analysis_archive/tree/8165649/DAQ/presto_vna_spectrum_20260826"
    now = "https://github.com/WashU-Astroparticle-Lab/analysis_archive/tree/b1e0501/DAQ/presto_vna_spectrum_20260826"
    d = _mkdir_with_meta(prev, [])
    try:
        _guard_out_dir(d, now, ["[Signed] presto_vna_spectrum_20260831 (copy)"])
    except SystemExit as exc:
        check("different LA pages in the same dir exits(4)", exc.code == 4)
    else:
        raise AssertionError("FAILED: guard did not block the real collision")


def test_guard_escape_hatches() -> None:
    d = _mkdir_with_meta("https://github.com/o/r/tree/abc/A", ["p"])
    other = "https://github.com/o/r/tree/abc/B"
    _guard_out_dir(d, other, ["q"], reuse=True)
    check("--reuse permits the write", True)
    _guard_out_dir(d, other, ["q"], force=True)
    check("--force permits the write", True)

    fresh = Path(tempfile.mkdtemp())
    _guard_out_dir(fresh, other, ["q"])
    check("empty directory is always allowed", True)

    bad = Path(tempfile.mkdtemp())
    (bad / "metadata.json").write_text("{not json", encoding="utf-8")
    _guard_out_dir(bad, other, ["q"])
    check("unreadable metadata does not block a run", True)


def test_preflight_fires_before_the_fetch() -> None:
    """The late guard protects the data but only after the fetch is paid for.

    In the real Aug 31 case that means ~35 s of fetching, and a Duo prompt if the
    cookie had expired, on behalf of a run that is about to be refused.
    """
    root = Path(tempfile.mkdtemp())
    prev = "https://github.com/o/r/tree/aaa/DAQ/presto_vna_spectrum_20260826"
    now = "https://github.com/o/r/tree/bbb/DAQ/presto_vna_spectrum_20260826"
    d = root / "presto_vna_spectrum_20260826"
    d.mkdir()
    (d / "metadata.json").write_text(
        json.dumps({"github_url": prev, "la_pages": []}), encoding="utf-8"
    )

    try:
        _preflight_guard(now, ["[Signed] presto_vna_spectrum_20260831 (copy)"], output_root=root)
    except SystemExit as exc:
        check("pre-flight refuses before any fetch happens", exc.code == 4)
    else:
        raise AssertionError("FAILED: pre-flight did not catch the collision")

    _preflight_guard(now, [], output_root=root)
    check("pre-flight allows a plain re-run at a newer commit", True)

    _preflight_guard(now, ["different page"], output_root=root, force=True)
    check("--force still bypasses the pre-flight", True)

    _preflight_guard(None, ["some page"], output_root=root)
    check("LabArchives-only runs skip the pre-flight (id not knowable yet)", True)

    _preflight_guard("https://github.com/o/r/tree/aaa/DAQ/never_seen", ["x"], output_root=root)
    check("an unseen folder is allowed", True)


def test_generic_folder_names_do_not_become_experiment_ids() -> None:
    """A GitHub URL pointing at a container directory must not name the run.

    Pointing at .../qpd_squat_jkid_run20260414/notebooks produced a run directory
    literally called `outputs/notebooks/` holding a 50 MB duplicate of the
    correctly named run, with a report titled "[UNSIGNED] notebooks".
    """
    for name in ("notebooks", "scripts", "data", "src", "analysis", "DAQ", "Main"):
        check(f"{name!r} is recognised as a container name",
              name.strip().lower() in _GENERIC_FOLDER_NAMES)

    for name in ("presto_vna_spectrum_20260826", "qpd_squat_jkid_run20260414",
                 "power_calibration_20260227"):
        check(f"{name!r} is NOT treated as generic", name.lower() not in _GENERIC_FOLDER_NAMES)

    pages = ["20260409 Cabling and Cooldown - SQUAT(LED), WashU QPD, (New) JKID5x5 (xray source)",
             "20260413 SQUAT"]
    derived = _la_pages_to_experiment_id(pages)
    check("the LabArchives-derived fallback is informative",
          derived == "20260409_cabling_cooldown_squat")


def test_parse_args() -> None:
    pos, opts = _parse_args(["https://github.com/o/r/tree/x/y", "page a", "--experiment-id", "my_run"])
    check("positionals survive option parsing", pos == ["https://github.com/o/r/tree/x/y", "page a"])
    check("--experiment-id is read", opts["experiment_id"] == "my_run")

    _, opts = _parse_args(["p", "--experiment-id=inline_run", "--reuse"])
    check("--opt=value form works", opts["experiment_id"] == "inline_run")
    check("--reuse sets the flag", opts["reuse"] is True)

    _, opts = _parse_args(["p", "--out-dir", "/tmp/somewhere", "--force"])
    check("--out-dir is read", opts["out_dir"] == "/tmp/somewhere")
    check("--force sets the flag", opts["force"] is True)

    pos, opts = _parse_args(["page only"])
    check("defaults are inert", pos == ["page only"] and not any(
        [opts["experiment_id"], opts["out_dir"], opts["reuse"], opts["force"]]
    ))


if __name__ == "__main__":
    test_gh_identity_ignores_ref()
    test_guard_allows_plain_rerun()
    test_guard_blocks_the_real_collision()
    test_guard_escape_hatches()
    test_preflight_fires_before_the_fetch()
    test_generic_folder_names_do_not_become_experiment_ids()
    test_parse_args()
    print(f"\ntest_out_dir_guard: {PASSED} checks passed")
