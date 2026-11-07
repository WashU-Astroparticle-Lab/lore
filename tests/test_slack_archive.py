"""The Slack figure archive (lab_agent.slack.archive): capture, group, summarise, post.

Nothing here reaches Slack, LabArchives or Claude: each is a stand-in. The rules checked are
the ones the lab asked for on 2026-11-07: figures and a summary of the discussion around them,
never people's messages copied onto the page; one page per week, added to every day; a
discussion posted once it has gone quiet, and a late reply as an update.

Run: python tests/run_all.py slack_archive
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from lab_agent.slack import archive as A

T0 = datetime(2026, 10, 29, 13, 0).timestamp()   # a Thursday


@contextlib.contextmanager
def patched(obj, **attrs):
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


def png(path: Path) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), (200, 30, 30)).save(path)
    return path


def msg(minutes: float, text: str, user="U1", image=False, replies=(), reactions=(), bot=False):
    ts = f"{T0 + minutes * 60:.6f}"
    m = {"ts": ts, "user": user, "text": text, "reactions": [{"name": r} for r in reactions]}
    if bot:
        m["bot_id"] = "B1"
    if image:
        m["files"] = [{"id": f"F{int(minutes)}", "name": f"plot{int(minutes)}.png",
                       "mimetype": "image/png", "filetype": "png",
                       "url_private_download": f"https://files/F{int(minutes)}"}]
    if replies:
        m["reply_count"] = len(replies)
    m["_replies"] = list(replies)
    return m


class FakeSlack:
    def __init__(self, messages, member=True):
        self.messages, self.member, self.joined, self.downloads = messages, member, [], []

    def channels(self):
        return {"data_analysis": {"id": "C1", "is_member": self.member}}

    def join(self, cid):
        self.joined.append(cid)

    def history(self, cid, oldest, latest):
        return [{k: v for k, v in m.items() if k != "_replies"} for m in self.messages
                if oldest <= float(m["ts"]) <= latest]

    def replies(self, cid, ts):
        m = next(m for m in self.messages if m["ts"] == ts)
        return [{k: v for k, v in r.items() if k != "_replies"} for r in m["_replies"]]

    def user_name(self, uid):
        return {"U1": "Alice Smith", "U2": "Bob Jones"}.get(uid)

    def download(self, url, dest):
        self.downloads.append(url)
        png(dest)


class FakeLA:
    def __init__(self, fail_attach_once=False):
        self.entries, self.attachments, self.pages = [], [], {}
        self.fail = fail_attach_once

    def week_page(self, state, monday):
        state["folder"] = {"nbid": "NB", "tree_id": "FOLDER"}
        self.pages.setdefault(monday, f"PAGE-{monday}")
        state["pages"][monday.isoformat()] = {"tree_id": self.pages[monday], "title": A.week_title(monday)}
        return self.pages[monday]

    def add_html(self, state, page, html):
        self.entries.append((page, html))

    def attach(self, state, page, path, name):
        if self.fail:
            self.fail = False
            raise TimeoutError("LabArchives timeout")
        self.attachments.append((page, name))


def fake_summary(prompt, images):
    return ("TITLE: Qi versus power for BE260416\n**What's shown**\n- Qi falls with power\n"
            "**What was concluded**\n- Alice will repeat it at base\n**IDs**: BE260416")


THREAD_REPLY = msg(30, "looks like TLS saturation to me", user="U2")
MESSAGES = [
    msg(-10, "running the power sweep now"),
    msg(0, "Qi vs power for BE260416", image=True, replies=[THREAD_REPLY]),
    msg(10, "and the phase", image=True),
    msg(300, "unrelated later plot", image=True),
    msg(400, "LORE posted a figure", image=True, bot=True),
    msg(600, "we decided the cable was the problem", reactions=["pushpin"]),
]


def test_figures_close_together_are_one_discussion_and_bots_are_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        A.capture(FakeSlack(MESSAGES), ["data_analysis"], T0 - 3600, T0 + 86400, root)
        recs = A.load_captured("data_analysis", T0 - 3600, T0 + 86400, root)
        ds = A.group("data_analysis", recs)
        first = ds[0]
        assert len(first.seeds) == 2 and len(first.image_files(root)) == 2, first.seeds
    assert [round((d.start - T0) / 60) for d in ds] == [0, 300, 600], [d.start for d in ds]
    texts = [m["text"] for m in first.messages()]
    assert "running the power sweep now" in texts, "the set-up message before the figure"
    assert "looks like TLS saturation to me" in texts, "the thread reply"
    assert not ds[2].figures, "a pinned text-only discussion has no figures"


def test_capture_downloads_figures_joins_and_merges_late_replies():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        slack = FakeSlack(MESSAGES, member=False)
        A.capture(slack, ["data_analysis"], T0 - 3600, T0 + 86400, root)
        assert slack.joined == ["C1"], "a channel it is not in is joined, not skipped"
        assert len(slack.downloads) == 4 and (root / "data_analysis" / "files" / "F0.png").exists()
        MESSAGES[1]["_replies"].append(msg(500, "confirmed at base", user="U2"))
        MESSAGES[1]["reply_count"] = 2
        try:
            A.capture(slack, ["data_analysis"], T0 - 3600, T0 + 86400, root)
        finally:
            MESSAGES[1]["_replies"].pop()
            MESSAGES[1]["reply_count"] = 1
        day = json.loads((root / "data_analysis" / "2026-10-29.json").read_text())
        seed = next(r for r in day if r["text"].startswith("Qi vs power"))
        assert [r["text"] for r in seed["replies"]][-1] == "confirmed at base"
        assert len(slack.downloads) == 4, "a figure already saved is not downloaded again"


def test_a_discussion_waits_until_it_has_gone_quiet():
    d = A.Discussion("c", [{"ts": str(T0)}], [{"ts": str(T0)}])
    assert not A.settled(d, T0 + 3600)
    assert A.settled(d, T0 + A.SETTLE_HOURS * 3600 + 1)


def test_weeks_start_on_monday():
    assert A.week_monday(T0) == date(2026, 10, 26)
    assert A.week_title(date(2026, 10, 26)) == "Slack — Week of Oct 26, 2026"


def test_the_summariser_gets_no_names_it_does_not_have_and_no_tools():
    d = A.Discussion("c", [], [{"ts": str(T0), "user": "U9", "text": "hi", "files": []}])
    t = A.transcript(d, {})
    assert "Member A" in t and "U9" not in t, t
    assert "Alice:" in A.transcript(A.Discussion("c", [], [{"ts": str(T0), "user": "U1",
                                                           "text": "x", "files": []}]),
                                   {"U1": "Alice Smith"})
    import inspect

    src = inspect.getsource(A.claude_vision)
    assert "claude_text_only_args" in src and "dangerously" not in src
    assert '"--input-format", "stream-json"' in src, "images go in as content, not via a tool"


def test_the_page_entry_holds_the_summary_not_the_messages():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        A.capture(FakeSlack(MESSAGES), ["data_analysis"], T0 - 3600, T0 + 86400, root)
        d = A.group("data_analysis", A.load_captured("data_analysis", T0 - 3600, T0 + 86400, root))[0]
        title, body = A.summarise(d, {"U1": "Alice Smith"}, root, ask=fake_summary)
        html = A.entry_html(d, title, body, ["Alice", "Bob"], ["data:image/jpeg;base64,AAA"])
    assert "Thu Oct 29 · #data_analysis · 13:00 — Qi versus power for BE260416" in html, html
    assert "summarised by LORE" in html and "no message is quoted" in html
    for verbatim in ("running the power sweep now", "looks like TLS saturation to me",
                     "and the phase"):
        assert verbatim not in html, f"a message was copied onto the page: {verbatim}"
    assert '<img src="data:image/jpeg;base64,AAA">' in html


def _state_file(tmp):
    return Path(tmp) / "state.json"


def test_a_daily_run_posts_once_and_resumes_a_half_posted_discussion():
    with tempfile.TemporaryDirectory() as tmp, patched(A, channels_configured=lambda: ["data_analysis"]):
        root, sf = Path(tmp) / "cap", _state_file(tmp)
        la = FakeLA(fail_attach_once=True)
        asked = []

        def ask(p, imgs):
            asked.append(len(imgs))
            return fake_summary(p, imgs)

        now = T0 + 2 * 86400
        code, log = A.run(src=FakeSlack(MESSAGES), target=la, now=now, ask=ask, root=root,
                          state_file=sf, capture_days=5, window_days=5)
        assert code == 5 and any("FAILED" in l for l in log), log   # attachment timed out
        code, log = A.run(src=FakeSlack(MESSAGES), target=la, now=now, ask=ask, root=root,
                          state_file=sf, capture_days=5, window_days=5)
        assert code == 0, log
        firsts = [h for _, h in la.entries if "13:00" in h]
        assert len(firsts) == 1, "the retry posted the entry again"
        assert len(asked) == 3, "a summary is written once, then reused on the retry"
        assert {p for p, _ in la.attachments} == {"PAGE-2026-10-26"}
        assert ("PAGE-2026-10-26", "2026-10-29_1300_data_analysis_1.png") in la.attachments
        state = A.load_state(sf)
        assert all(r["status"] == "posted" for r in state["discussions"].values())
        before = len(la.entries)
        A.run(src=FakeSlack(MESSAGES), target=la, now=now, ask=ask, root=root, state_file=sf,
              capture_days=5, window_days=5)
        assert len(la.entries) == before, "a posted discussion was posted again"


def test_a_late_reply_becomes_an_update_on_the_same_week_page():
    with tempfile.TemporaryDirectory() as tmp, patched(A, channels_configured=lambda: ["data_analysis"]):
        root, sf, la = Path(tmp) / "cap", _state_file(tmp), FakeLA()
        now = T0 + 2 * 86400
        A.run(src=FakeSlack(MESSAGES), target=la, now=now, ask=fake_summary, root=root,
              state_file=sf, capture_days=5, window_days=5)
        late = msg(60 * 24 * 3, "re-measured at base: TLS confirmed", user="U2")
        MESSAGES[1]["_replies"].append(late)
        MESSAGES[1]["reply_count"] = 2
        prompts = []
        try:
            A.run(src=FakeSlack(MESSAGES), target=la, now=T0 + 5 * 86400,
                  ask=lambda p, i: prompts.append(p) or "TITLE: Update\n- TLS confirmed",
                  root=root, state_file=sf, capture_days=8, window_days=8)
        finally:
            MESSAGES[1]["_replies"].pop()
            MESSAGES[1]["reply_count"] = 1
        update = [h for p, h in la.entries if "Update" in h]
        assert len(update) == 1 and la.entries[-1][0] == "PAGE-2026-10-26", la.entries[-1][0]
        assert "discussion of Oct 29" in update[0]
        assert "Earlier summary" in prompts[0] and "re-measured at base" in prompts[0]


def test_names_are_looked_up_again_once_the_scope_is_added():
    class NoScope(FakeSlack):
        def user_name(self, uid):
            raise A.MissingScope("users:read")

    with tempfile.TemporaryDirectory() as tmp, patched(A, channels_configured=lambda: ["data_analysis"]):
        root, sf, prompts = Path(tmp) / "cap", _state_file(tmp), []
        ask = lambda p, i: prompts.append(p) or fake_summary(p, i)
        A.run(src=NoScope(MESSAGES[:3]), target=FakeLA(), now=T0 + 86400, ask=ask, root=root,
              state_file=sf, capture_days=5, window_days=5)
        assert A.load_state(sf).get("names_unavailable")
        A.run(src=FakeSlack(MESSAGES), target=FakeLA(), now=T0 + 2 * 86400, ask=ask,
              root=root, state_file=sf, capture_days=5, window_days=5)
        assert "Alice" in prompts[-1], "the first run's missing scope stuck for good"


def test_preview_posts_nothing_and_saves_no_state():
    with tempfile.TemporaryDirectory() as tmp, patched(A, channels_configured=lambda: ["data_analysis"]):
        root, sf, out = Path(tmp) / "cap", _state_file(tmp), Path(tmp) / "preview"
        la = FakeLA()
        A.run(src=FakeSlack(MESSAGES), target=la, now=T0 + 2 * 86400, ask=fake_summary,
              root=root, state_file=sf, preview_dir=out, capture_days=5, window_days=5)
        assert not la.entries and not sf.exists()
        page = (out / "2026-10-26.html").read_text(encoding="utf-8")
        assert page.count("summarised by LORE") == 3, page[:300]


def test_the_graph_crawls_the_slack_folder_but_not_the_reports_beside_it():
    from lab_agent.collect import la_crawl

    class LA:
        def _get_tree_level(self, nbid, parent="0"):
            tree = {"0": [("AI Agent", "AI", False), ("Notes", "P0", True)],
                    "AI": [("[UNSIGNED] a report", "R1", True), ("Slack", "SL", False)],
                    "SL": [("Slack — Week of Oct 26, 2026", "W1", True)]}
            root = ET.Element("r")
            for name, tid, page in tree.get(parent, []):
                n = ET.SubElement(root, "level-node")
                ET.SubElement(n, "tree-id").text = tid
                ET.SubElement(n, "display-text").text = name
                ET.SubElement(n, "is-page").text = "true" if page else "false"
            return root.findall(".//level-node")

    pages = la_crawl._enumerate_pages(LA(), "NB", exclude={"ai agent"}, include_within={"slack"})
    assert sorted(p for p, _ in pages) == ["Notes", "Slack — Week of Oct 26, 2026"], pages


def test_placeholder_names_never_reach_the_page():
    # Verbatim shapes from a real preview, where the rule alone did not hold.
    cases = {
        "- Stycast is delaminating; a lab member (Member A) flagged it":
            "- Stycast is delaminating; a lab member flagged it",
        "- Member B advised Dana to use a Geiger counter":
            "- A lab member advised Dana to use a Geiger counter",
        "- The bias tee will be removed, confirmed by Members B and C":
            "- The bias tee will be removed, confirmed by lab members",
        "Bags were located by Member C.": "Bags were located by a lab member.",
    }
    for raw, want in cases.items():
        assert A.unlabel(raw) == want, (raw, A.unlabel(raw))
    title, body = A.parse_summary("TITLE: Fix by Member A\n- raised by Member A")
    assert "Member" not in title + body, (title, body)


def test_a_discussion_stops_at_the_next_ones_figure():
    recs = [{"ts": f"{T0 + m * 60:.6f}", "user": "U1", "text": t, "reactions": [], "bot": False,
             "files": [{"id": f"F{m}", "name": "p.png", "mimetype": "image/png"}] if img else []}
            for m, t, img in ((0, "first figure", True), (60, "talk", False),
                              (101, "second figure", True), (120, "more", False))]
    a, b = A.group("c", recs)
    assert [r["text"] for r in a.context] == ["first figure", "talk"], a.context
    assert [r["text"] for r in b.context] == ["second figure", "more"], b.context


def test_a_reply_also_sent_to_the_channel_is_counted_once():
    def rec(m, text, fid, replies=()):
        return {"ts": f"{T0 + m * 60:.6f}", "user": "U1", "text": text, "reactions": [],
                "bot": False, "replies": list(replies),
                "files": [{"id": fid, "name": "p.png", "mimetype": "image/png"}]}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for fid in ("F1", "F2"):
            png(root / f"{fid}.png")
        reply = rec(5, "the baseline after the fix", "F2")
        recs = [rec(0, "baseline before", "F1", [reply]), dict(reply)]
        for r in recs + [reply]:
            r["files"][0]["path"] = f"{r['files'][0]['id']}.png"
        (d,) = A.group("c", recs)
        assert [f["id"] for _, f, _ in d.image_files(root)] == ["F1", "F2"]
        assert A.transcript(d, {}).count("the baseline after the fix") == 1


def test_an_api_posted_image_is_kept_out_of_the_figure_manifest():
    from lab_agent.sources.labarchives.images import extract_embedded_img_urls

    xml = ('<r><entry><entry-data>&lt;img src="data:image/jpeg;base64,AAAA"&gt;'
           '&lt;img src="https://api.labarchives.com/attachments/inline_image/x?file_name=1.png"&gt;'
           '</entry-data></entry></r>')
    assert extract_embedded_img_urls(xml) == [
        "https://api.labarchives.com/attachments/inline_image/x?file_name=1.png"]


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
