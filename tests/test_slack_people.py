"""Sessions know who is talking (lab_agent.slack.people and the spawn prompt).

A lab member asked LORE to look something up for a colleague who had asked just above.
The session was told only the sender's Slack ID, the thread was "[User]" lines, and it
never saw the colleague's question. Its reply suggested asking someone, by name, who was
the person asking. These tests use made-up names and IDs and no Slack calls.

Run: python tests/run_all.py slack_people
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

from lab_agent.slack import history, people
from lab_agent.slack import sessions as s

NAMES = {"UASKER01": "Kai Rivers", "UCOLL001": "Dana Moss"}
BOT = "UBOT0001"


@contextlib.contextmanager
def patched(mod, **attrs):
    saved = {k: getattr(mod, k) for k in attrs}
    for k, v in attrs.items():
        setattr(mod, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(mod, k, v)


@contextlib.contextmanager
def directory(names=NAMES):
    """People's names from a dict, a fresh cache, and the bot's own ID."""
    calls = []

    def lookup(uid):
        calls.append(uid)
        return names.get(uid)

    with tempfile.TemporaryDirectory() as tmp:
        people._memory.clear()
        with patched(people, _lookup=lookup, CACHE_PATH=Path(tmp) / "names.json"), \
                patched(people.api, BOT_USER_ID=BOT):
            try:
                yield calls
            finally:
                people._memory.clear()


def test_ids_become_names_and_unknown_ones_stay_ids():
    with directory() as calls:
        assert people.label("UASKER01") == "Kai Rivers (<@UASKER01>)"
        assert people.label("UNOBODY1") == "<@UNOBODY1>", "an unknown ID is left as it is"
        assert people.label(BOT) == "LORE (you)" and people.label("X", bot=True) == "LORE (you)"
        assert people.named(f"<@{BOT}> ask <@UCOLL001> about it") == (
            f"<@{BOT}> (LORE, you) ask <@UCOLL001> (Dana Moss) about it")
        assert people.first_name("UASKER01") == "Kai"
        people.label("UASKER01")
        assert calls.count("UASKER01") == 1, "a name is looked up once, then cached"
        people._memory.clear()
        assert people.name_of("UASKER01") == "Kai Rivers" and calls.count("UASKER01") == 1, (
            "the disk cache answers after a restart")


def test_the_thread_says_who_wrote_each_message():
    msgs = [{"ts": "1.0", "user": "UCOLL001", "text": "Where are the Stycast mix ratios?"},
            {"ts": "2.0", "bot_id": "B1", "user": BOT, "text": "Looking."},
            {"ts": "3.0", "user": "UASKER01", "text": f"<@{BOT}> find it"}]
    with directory(), patched(history.api, slack_get=lambda *a, **k: {"ok": True, "messages": msgs}):
        out = history.fetch_history("C1", "1.0", current_ts="3.0")
    assert out.splitlines() == ["[Dana Moss (<@UCOLL001>)]: Where are the Stycast mix ratios?",
                                "[LORE (you)]: Looking."], out


def test_a_top_level_mention_sees_what_was_just_said_above_it():
    # conversations.history returns newest first.
    msgs = [{"ts": "1000.0", "subtype": "channel_join", "user": BOT, "text": "has joined"},
            {"ts": "940.0", "user": "UCOLL001", "text": "Anyone know the Stycast mix ratios?",
             "reply_count": 2}]
    seen = {}

    def slack_get(endpoint, params):
        seen.update(params, endpoint=endpoint)
        return {"ok": True, "messages": msgs}

    with directory(), patched(history.api, slack_get=slack_get):
        out = history.fetch_channel_before("C1", "1300.0")
    assert out == ("[Dana Moss (<@UCOLL001>), 6 min earlier]: Anyone know the Stycast mix "
                   "ratios? [2 replies in its thread]"), out
    assert seen["endpoint"] == "conversations.history" and seen["latest"] == "1300.0"


def _prompt(**kw) -> str:
    launched = []
    defaults = dict(user="UASKER01", text=f"<@{BOT}> find stycast mix ratio if in log",
                    channel="C1", thread_ts=None, current_ts="1300.0")
    defaults.update(kw)
    channel_calls = []
    with directory(), patched(s, resumable_session=lambda k: None,
                              fetch_history=lambda *a: "(new conversation)",
                              fetch_channel_before=lambda *a: channel_calls.append(a) or
                              "[Dana Moss (<@UCOLL001>), 55 min earlier]: Anyone know the "
                              "Stycast mix ratios?",
                              _launch=lambda *a, **k: launched.append(a[4])):
        s.spawn_claude(defaults["user"], defaults["text"], defaults["channel"],
                       defaults["thread_ts"], defaults["current_ts"], is_dm=kw.get("is_dm", False))
    return launched[0], channel_calls


def test_the_session_is_told_who_is_asking_and_what_they_are_answering():
    prompt, calls = _prompt()
    assert "From          : Kai Rivers (<@UASKER01>)" in prompt, prompt[:600]
    assert "Slack user ID" not in prompt
    assert f"Latest message: <@{BOT}> (LORE, you) find stycast" in prompt
    assert "You are talking to Kai" in prompt and "Never tell them to ask themselves" in prompt
    assert "Dana Moss (<@UCOLL001>), 55 min earlier]: Anyone know the Stycast" in prompt
    assert len(calls) == 1


def test_a_dm_or_a_thread_reply_gets_no_channel_backlog():
    _, calls = _prompt(is_dm=True)
    assert calls == [], "a DM has no channel above it"
    _, calls = _prompt(thread_ts="900.0")
    assert calls == [], "a reply in a thread already has the thread"


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
