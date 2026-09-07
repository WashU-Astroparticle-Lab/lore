"""A draft's numbers must be checkable against the report it summarises.

The report pipeline gates its own Slack summary (critic item 10), but a message
*drafted* for a channel had no check at all. These tests also pin the mistake the
first version made: pointed at a whole run directory it passed everything,
because notebooks.md and the extractions hold tens of thousands of numbers, so
any value "appears somewhere".
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cli.verify_claims import _source_text, numbers_in, unsupported

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


REPORT = """# [UNSIGNED] exp

At 6.436588 GHz the difference is +0.01 to +0.15 dBm; at 6.9 GHz it is
+0.13 to +0.35 dBm. Presto spurs sit between -75 and -83 dBm. The VNA needed
a ~+4 dBm software offset.
"""


def _run_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "[UNSIGNED] exp.md").write_text(REPORT, encoding="utf-8")
    # The trap: a huge raw dump in the same directory must NOT be used as a source.
    (d / "notebooks.md").write_text(
        " ".join(str(n / 10) for n in range(20000)), encoding="utf-8")
    return d


def test_number_extraction() -> None:
    nums = numbers_in("agree to within +0.01 to −0.35 dBm at 6.9 GHz")
    check("signs are normalised away", "0.01" in nums and "0.35" in nums)
    check("unicode minus is handled", all(not n.startswith("−") for n in nums))
    check("bare small integers are ignored as non-evidence", "6" not in nums)
    check("decimals survive intact", "6.9" in nums)


def test_real_numbers_pass() -> None:
    d = _run_dir()
    draft = ("Presto and VNA agree to within +0.01 to +0.35 dBm; spurs sit between "
             "−75 and −83 dBm after a ~+4 dBm offset.")
    check("numbers quoted from the report pass", unsupported(draft, _source_text([str(d)])) == [])


def test_invented_numbers_fail() -> None:
    d = _run_dir()
    draft = "Spurs sit between −62 and −71 dBm after a ~+7.5 dBm offset."
    missing = unsupported(draft, _source_text([str(d)]))
    check("invented values are caught", set(missing) == {"62", "71", "7.5"})


def test_raw_dumps_are_not_used_as_a_source() -> None:
    """The first version's bug: 62/71/7.5 all passed because notebooks.md had them."""
    d = _run_dir()
    text = _source_text([str(d)])
    check("the report is used", "6.436588" in text)
    check("the raw notebook dump is NOT used", "1999.9" not in text)
    check("an invented value stays caught despite the dump existing",
          unsupported("offset was 7.5 dBm", text) == ["7.5"])


def test_wrong_label_is_NOT_caught() -> None:
    """Stated honestly so nobody trusts this further than it goes.

    "+0.01 to +0.35 at both tones" is wrong — those are two different tones'
    ranges merged — but every number in it is real, so presence-checking passes.
    """
    d = _run_dir()
    draft = "Both tones agree to within +0.01 to +0.35 dBm."
    check("a real number on the wrong label passes — read for pairing yourself",
          unsupported(draft, _source_text([str(d)])) == [])


if __name__ == "__main__":
    test_number_extraction()
    test_real_numbers_pass()
    test_invented_numbers_fail()
    test_raw_dumps_are_not_used_as_a_source()
    test_wrong_label_is_NOT_caught()
    print(f"\ntest_verify_claims: {PASSED} checks passed")
