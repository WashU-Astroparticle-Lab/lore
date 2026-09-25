"""A GitHub folder's notes reach the analysts, and a measurement agent's are marked as such.

Before this, .md files in a repository were fetched and then written nowhere, so a
README or the DAQ PC agent's run notes in <run>/Agent/ never reached a report. Now
they go to repo_notes.md, each headed by who wrote it, and an AI agent's notes are
labelled as unreviewed leads, like LORE's own [UNSIGNED] drafts.

Run: python tests/run_all.py repo_notes   (or: python tests/test_repo_notes.py)
"""
from __future__ import annotations

import sys

from lab_agent.collect.discover import (AGENT_NOTES_DESCRIPTION, HUMAN_NOTES_DESCRIPTION,
                                        REPO_NOTE_CHAR_LIMIT, classify_file,
                                        format_repo_notes, is_agent_notes)
from lab_agent.models import CollectedArtifact

AGENT_MD = "DAQ/PRIMA_JKID_JPLQPD_20260831/Agent/run_summary.md"


def note(path: str, text: str, source: str = "github") -> CollectedArtifact:
    kind, desc = classify_file(path)
    return CollectedArtifact(path=path, kind=kind, description=desc, content=text, source=source)


def test_the_measurement_agents_notes_are_recognised_by_where_they_live():
    for path in (AGENT_MD, "run/agents/log.txt", "run/agent_notes.md", "run/AGENT.md",
                 "notes/Agent/overnight.md"):
        assert is_agent_notes(path), path
        assert classify_file(path) == ("notes", AGENT_NOTES_DESCRIPTION), path
    for path in ("README.md", "notes/overnight.md", "run/agenda.md", "Agent/sweep.py",
                 "run/management.md"):
        assert not is_agent_notes(path), path
    assert classify_file("README.md") == ("notes", HUMAN_NOTES_DESCRIPTION)
    assert classify_file("Agent/sweep.py")[0] == "code", "only notes files are agent notes"


def test_repo_notes_carry_who_wrote_them_people_first():
    md = format_repo_notes([
        note(AGENT_MD, "LED at 99 mA gave about 6.9 keV (my estimate)."),
        note("README.md", "Sweep: 7.4-8.8 GHz, -40 dBm at the device."),
        note("LA page", "notebook text", source="labarchives"),
    ])
    assert md is not None
    assert md.index("# README.md") < md.index(f"# {AGENT_MD}"), "people's notes come first"
    agent_part = md[md.index(f"# {AGENT_MD}"):]
    assert "AI measurement agent, unreviewed" in agent_part
    assert "never as the source of a value" in agent_part
    assert "Written by people" in md[:md.index(f"# {AGENT_MD}")]
    assert "notebook text" not in md, "LabArchives notes belong in labarchives.md"


def test_nothing_to_write_means_no_file_and_long_notes_are_cut_visibly():
    assert format_repo_notes([note("run.py", "print(1)")]) is None
    assert format_repo_notes([CollectedArtifact(path="README.md", kind="notes", source="github",
                                                exists=False)]) is None
    long = format_repo_notes([note(AGENT_MD, "x" * (REPO_NOTE_CHAR_LIMIT + 50))])
    assert f"truncated at {REPO_NOTE_CHAR_LIMIT}" in long


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
