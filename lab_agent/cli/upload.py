"""
Upload a finished experiment report to LabArchives (invoked via the root-level
upload_to_labarchives.py shim).

Usage:
    python upload_to_labarchives.py <out_dir> [options]

Options:
    --report-file NAME   upload this report instead of the newest "[UNSIGNED] *.md".
                         Absolute, or relative to <out_dir>. Use it whenever the
                         directory holds more than one report.
    --page-title TITLE   page title in LabArchives (default: "[UNSIGNED] <out_dir name>")
    --new-page           always create a new page, even if one with this title
                         exists. Default is to add a revision to the existing page,
                         so a re-upload does not leave duplicate pages behind.
    --force              upload even though the critic did not pass the report.
                         Use only after reading critique.md and disagreeing with it.

Before uploading, this refuses to run unless <out_dir>/critique.md reports
`passed` (see lab_agent/critique.py for why that is enforced here rather than
left to the pipeline instructions), and it fills in the report's
"LabArchives: <folder> / <page title>" footer, which the report-writer cannot
know at write time.

Example:
    python upload_to_labarchives.py outputs/power_calibration_20260227
    python upload_to_labarchives.py outputs/presto_vna_spectrum_20260831 \
        --report-file "[UNSIGNED] presto_vna_spectrum_20260831_simple.md"

The report must already exist in <out_dir> (newest "[UNSIGNED] *.md", or
experiment_report.md as a fallback).
"""
from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

from ..config import PROJECT_ROOT, load_env
from ..critique import blocks_upload, explain, read_verdict
from ..publish import upload_report
from ..publish.labarchives import _upload_folder_name, resolve_report_path


_FOOTER = re.compile(r"^(\s*)LabArchives:.*$", re.MULTILINE)


def fix_location_footer(paths: Iterable[Path], folder: str, page_title: str) -> list[Path]:
    """Rewrite the `LabArchives: <folder> / <page title>` line in each file.

    report-writer.md asks the writer to end the report with where it lives, but
    the writer runs *before* the upload and cannot know. Asked for a fact it
    could not have, it repeated the source page name twice — the real run
    shipped "LabArchives: 20250904 Standalone Warm Amp Noise Digest /
    20250904 Standalone Warm Amp Noise Digest" to the lab, naming the notes page
    it read instead of the report it had just written.

    Both values are known here, before anything is posted, so the footer is
    filled in for the uploaded document *and* for slack_summary.md.
    """
    line = f"LabArchives: {folder} / {page_title}"
    changed: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        patched, n = _FOOTER.subn(lambda m: f"{m.group(1)}{line}", text)
        if n and patched != text:
            path.write_text(patched, encoding="utf-8")
            changed.append(path)
    return changed


def _parse_args(argv: list[str]) -> tuple[str | None, dict]:
    opts: dict = {"report_file": None, "page_title": None, "new_page": False,
                  "force": False}
    positional: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        key, _, inline = arg.partition("=")
        if key in ("--report-file", "--page-title"):
            field = key.lstrip("-").replace("-", "_")
            if inline:
                opts[field] = inline
                i += 1
            elif i + 1 < len(argv):
                opts[field] = argv[i + 1]
                i += 2
            else:
                print(f"[upload] {key} needs a value")
                sys.exit(1)
            continue
        if key in ("--new-page", "--force"):
            opts[key.lstrip("-").replace("-", "_")] = True
            i += 1
            continue
        positional.append(arg)
        i += 1
    return (positional[0] if positional else None), opts


def main() -> None:
    # The blocked-upload message contains em dashes; the Windows console is
    # cp1252 and a UnicodeEncodeError here would replace a clear refusal with a
    # traceback. verify_claims hit exactly this printing a minus sign.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    load_env()

    target, opts = _parse_args(sys.argv[1:])
    if not target:
        print(__doc__)
        sys.exit(1)

    out_dir = Path(target)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir

    page_title = opts["page_title"] or f"[UNSIGNED] {out_dir.name}"

    verdict = read_verdict(out_dir)
    if blocks_upload(verdict):
        if not opts["force"]:
            print(explain(verdict))
            sys.exit(5)
        print(f"[upload] --force: uploading despite '{verdict.outcome}' "
              f"{verdict.detail}".rstrip())

    try:
        report_path = resolve_report_path(out_dir, opts["report_file"])
    except FileNotFoundError as exc:
        print(f"[upload] {exc}")
        sys.exit(1)
    for changed in fix_location_footer(
        [report_path, out_dir / "slack_summary.md"], _upload_folder_name(), page_title
    ):
        print(f"[upload] filled in the LabArchives footer: {changed.name}")

    upload_report(
        out_dir,
        page_title,
        report_file=opts["report_file"],
        new_page=opts["new_page"],
    )


if __name__ == "__main__":
    main()
