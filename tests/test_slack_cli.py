"""Stage C/D4 — the Slack CLI surface and file-upload wake-up.

Covers the parts that need no network:
  * CLI argument parsing (message text comes from a FILE, never a shell argument,
    which is what stopped backticks in a message being executed by bash);
  * api_get/api_post raising on Slack's ok:false — an HTTP 200 with
    {"ok": false} used to be indistinguishable from a delivered message;
  * dm_event_text, which decides whether a DM event wakes the bot. A file upload
    arrives with subtype "file_share" and was dropped, so "just drop it in the
    thread and I'll start" was a promise the listener could not keep.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lab_agent.cli.slack import _parse
from lab_agent.slack import api

PASSED = 0


def check(label: str, cond: bool) -> None:
    global PASSED
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    PASSED += 1
    print(f"  ok  {label}")


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(body: dict):
    """Make every Slack call return *body*; returns a restore callable."""
    original = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _FakeResponse(json.dumps(body).encode())
    return lambda: setattr(urllib.request, "urlopen", original)


def test_cli_parsing() -> None:
    cmd, opts = _parse(["post", "--channel", "C1", "--thread", "123.45", "--text-file", "m.txt"])
    check("post parses channel/thread/text-file",
          cmd == "post" and opts["channel"] == "C1" and opts["thread"] == "123.45"
          and opts["text_file"] == "m.txt")

    cmd, opts = _parse(["upload", "--channel=C1", "--file", "a.png", "--file", "b.png"])
    check("--file repeats into a list", opts["file"] == ["a.png", "b.png"])
    check("--opt=value form works", opts["channel"] == "C1")

    _, opts = _parse(["read-thread", "--channel", "C1", "--thread", "1.2"])
    check("read-thread has a default limit", opts["limit"] == "50")

    _, opts = _parse(["channels"])
    check("channels needs no options", opts["file"] == [])

    # There is deliberately no --text: message bodies never travel as shell args.
    _, opts = _parse(["post", "--channel", "C1", "--text-file", "m.txt"])
    check("no --text option is produced", "text" not in opts)


def test_api_raises_on_not_ok() -> None:
    restore = _patch_urlopen({"ok": False, "error": "channel_not_found"})
    try:
        try:
            api.api_post("chat.postMessage", {"channel": "C1", "text": "hi"})
        except api.SlackError as exc:
            check("api_post raises on ok:false", "channel_not_found" in str(exc))
        else:
            raise AssertionError("FAILED: api_post accepted ok:false")
    finally:
        restore()

    restore = _patch_urlopen({"ok": False, "error": "missing_scope",
                              "needed": "groups:read", "provided": "chat:write"})
    try:
        try:
            api.api_get("conversations.list", {})
        except api.SlackError as exc:
            check("a scope error names the missing scope", "groups:read" in str(exc))
        else:
            raise AssertionError("FAILED: api_get accepted ok:false")
    finally:
        restore()

    restore = _patch_urlopen({"ok": True, "ts": "1788.99"})
    try:
        ts = api.post_message_checked("C1", None, "hello")
        check("a successful post returns its ts", ts == "1788.99")
    finally:
        restore()

    restore = _patch_urlopen({"ok": True})  # ok but no ts
    try:
        try:
            api.post_message_checked("C1", None, "hello")
        except api.SlackError:
            check("ok without a ts is still a failure", True)
        else:
            raise AssertionError("FAILED: accepted ok-without-ts")
    finally:
        restore()


def test_upload_rejects_empty_file() -> None:
    empty = Path(tempfile.mkdtemp()) / "empty.png"
    empty.write_bytes(b"")
    try:
        api.upload_file("C1", None, empty)
    except api.SlackError as exc:
        check("an empty file is refused before any API call", "empty" in str(exc).lower())
    else:
        raise AssertionError("FAILED: empty file was uploaded")


def _patch_urlopen_sequence(bodies: list[dict]):
    """Return each body in turn; restores the original on call."""
    original = urllib.request.urlopen
    queue = list(bodies)

    def fake(*a, **k):
        return _FakeResponse(json.dumps(queue.pop(0)).encode())

    urllib.request.urlopen = fake
    return lambda: setattr(urllib.request, "urlopen", original)


def test_list_channels_falls_back_to_public() -> None:
    """Slack rejects the whole conversations.list call if ANY requested type
    lacks scope, so asking for public+private turns a missing 'groups:read' into
    "no channels at all" — and then the agent asks the user which channel, four
    times, with the list one command away."""
    restore = _patch_urlopen_sequence([
        {"ok": False, "error": "missing_scope", "needed": "groups:read", "provided": "channels:read"},
        {"ok": True, "channels": [
            {"id": "C1", "name": "data_analysis", "is_member": True, "is_private": False},
        ]},
    ])
    try:
        channels, warning = api.list_channels()
        check("public channels still come back without groups:read", len(channels) == 1)
        check("the fallback names the missing scope", warning and "groups:read" in warning)
    finally:
        restore()

    restore = _patch_urlopen_sequence([
        {"ok": True, "channels": [
            {"id": "C1", "name": "pub", "is_member": True, "is_private": False},
            {"id": "C2", "name": "priv", "is_member": True, "is_private": True},
        ]},
    ])
    try:
        channels, warning = api.list_channels()
        check("with full scope both types are listed", len(channels) == 2)
        check("no warning when nothing was skipped", warning is None)
    finally:
        restore()

    # A non-scope failure must still surface rather than being swallowed.
    restore = _patch_urlopen_sequence([{"ok": False, "error": "ratelimited"}])
    try:
        api.list_channels()
    except api.SlackError as exc:
        check("unrelated failures still raise", "ratelimited" in str(exc))
    else:
        raise AssertionError("FAILED: swallowed a non-scope error")
    finally:
        restore()


def test_file_is_shared_survives_the_indexing_race() -> None:
    """completeUploadExternal returns before Slack indexes the share.

    A single immediate files.info check reported three files "uploaded but not
    visible" while they were already in the thread — and the agent then told the
    user there was a Slack permission problem, which there was not. Failing
    closed on a race is worse than not checking.
    """
    # First two polls see nothing; the third sees the DM share.
    restore = _patch_urlopen_sequence([
        {"ok": True, "file": {"channels": [], "groups": [], "ims": []}},
        {"ok": True, "file": {"channels": [], "groups": [], "ims": []}},
        {"ok": True, "file": {"channels": [], "groups": [], "ims": ["D0ARUL9EDKR"]}},
    ])
    try:
        check("a slow share is found on retry",
              api.file_is_shared("F1", "D0ARUL9EDKR", attempts=4, delay=0) is True)
    finally:
        restore()

    # A DM share can appear under shares.private rather than ims.
    restore = _patch_urlopen_sequence([
        {"ok": True, "file": {"shares": {"private": {"D0ARUL9EDKR": [{"ts": "1.2"}]}}}},
    ])
    try:
        check("shares.private counts as shared",
              api.file_is_shared("F1", "D0ARUL9EDKR", attempts=1, delay=0) is True)
    finally:
        restore()

    # A genuinely absent file must still come back False.
    restore = _patch_urlopen_sequence([
        {"ok": True, "file": {"channels": ["C999"], "ims": []}},
    ])
    try:
        check("a file shared elsewhere is still not in this channel",
              api.file_is_shared("F1", "D0ARUL9EDKR", attempts=1, delay=0) is False)
    finally:
        restore()


def test_dm_event_text() -> None:
    check("plain text wakes the bot", api.dm_event_text({"text": "hello"}) == "hello")
    check("bot echoes are ignored", api.dm_event_text({"text": "hi", "bot_id": "B1"}) is None)
    check("edits and joins are ignored",
          api.dm_event_text({"text": "x", "subtype": "message_changed"}) is None)

    # The real regression: a photo with no caption.
    got = api.dm_event_text({"subtype": "file_share", "text": "",
                             "files": [{"name": "IMG_4708.jpg"}], "ts": "1788712059.316019"})
    check("a text-less file upload wakes the bot", got is not None)
    check("the file name reaches the session", "IMG_4708.jpg" in (got or ""))
    # Observed live: without the ts the session reaches for thread_ts, gets
    # nothing back, and needs a read-thread round-trip to find the real message.
    check("the file message's own ts reaches the session too",
          "1788712059.316019" in (got or ""))

    got = api.dm_event_text({"subtype": "file_share", "text": "here you go",
                             "files": [{"name": "a.png"}, {"name": "b.png"}]})
    check("caption and both file names survive",
          got is not None and "here you go" in got and "a.png" in got and "b.png" in got)

    check("a genuinely empty message is ignored", api.dm_event_text({"text": "   "}) is None)
    check("slack link wrapping is undone",
          api.dm_event_text({"text": "<https://github.com/x/y>"}) == "https://github.com/x/y")


if __name__ == "__main__":
    test_cli_parsing()
    test_api_raises_on_not_ok()
    test_upload_rejects_empty_file()
    test_list_channels_falls_back_to_public()
    test_file_is_shared_survives_the_indexing_race()
    test_dm_event_text()
    print(f"\ntest_slack_cli: {PASSED} checks passed")
