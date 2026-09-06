"""Unit tests for Slack message-text unwrapping (lab_agent.slack.api).

Slack auto-links URLs in a message's `text` as <url> or <url|label> and
HTML-escapes &, <, >. These tests pin the normalization that lets a GitHub URL
sent via Slack reach run.py as a bare URL instead of "<https://...>".

Run: python -m pytest tests/test_slack_unwrap.py   (or: python tests/test_slack_unwrap.py)
"""
from lab_agent.slack.api import unwrap_slack_text


def test_bare_wrapped_url():
    assert (
        unwrap_slack_text("<https://github.com/WashU-Astroparticle-Lab/daq/tree/main/squat_run>")
        == "https://github.com/WashU-Astroparticle-Lab/daq/tree/main/squat_run"
    )


def test_wrapped_url_with_label():
    # Slack renders <url|label> when the message shows display text; keep the URL.
    assert (
        unwrap_slack_text("see <https://github.com/org/repo/tree/main|the repo> please")
        == "see https://github.com/org/repo/tree/main please"
    )


def test_url_plus_page_titles_realistic_message():
    msg = '<https://github.com/org/repo/tree/main/exp> "Page One" "Page Two"'
    assert unwrap_slack_text(msg) == (
        'https://github.com/org/repo/tree/main/exp "Page One" "Page Two"'
    )


def test_leading_char_is_http_after_unwrap():
    # run.py routes on args[0].startswith("http"); the leading '<' must be gone.
    out = unwrap_slack_text("<https://github.com/org/repo/tree/main>")
    assert out.startswith("http")


def test_entities_unescaped():
    assert unwrap_slack_text("a &amp; b &lt; c &gt; d") == "a & b < c > d"


def test_escaped_lt_roundtrips():
    # A literal "&lt;" typed by the user is sent by Slack as "&amp;lt;".
    assert unwrap_slack_text("&amp;lt;") == "&lt;"


def test_user_and_channel_mentions_untouched():
    assert unwrap_slack_text("<@U123> and <#C456|general> and <!here>") == (
        "<@U123> and <#C456|general> and <!here>"
    )


def test_plain_text_unchanged():
    assert unwrap_slack_text("how's the DR right now?") == "how's the DR right now?"


def test_empty_and_none_safe():
    assert unwrap_slack_text("") == ""
    assert unwrap_slack_text(None) is None


if __name__ == "__main__":
    import sys

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
