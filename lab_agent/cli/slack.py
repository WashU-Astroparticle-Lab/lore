"""
Slack from the pipeline — the ONE supported way for an agent to talk to Slack.

    python -m lab_agent.cli.slack post        --channel C123 [--thread TS] --text-file msg.txt
    python -m lab_agent.cli.slack upload      --channel C123 [--thread TS] --file a.png [--file b.png]
                                              [--comment-file note.txt] [--title "..."]
    python -m lab_agent.cli.slack read-thread --channel C123 --thread TS [--limit 50]
    python -m lab_agent.cli.slack channels    [--filter kid]
    python -m lab_agent.cli.slack search      --query "presto vna" [--limit 20]
    python -m lab_agent.cli.slack fetch-files --channel C123 --ts 1788391397.681369 [--out DIR]

Why this exists: every session used to hand-roll these calls as `python -c "..."`
one-liners and got them wrong in a different way each time — backticks in the
message text were executed by the shell (words silently vanished from messages
users received, and one failure printed a session cookie), Windows paths broke
the Python literal, `files.upload` answered `method_deprecated`, and a rejected
call looked exactly like a delivered one. Message text is therefore only ever
read from a FILE, never from a shell argument, and every command verifies by
reading back what it did before reporting success.

Exit codes: 0 delivered and verified, 1 usage error, 2 the Slack call failed.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from ..config import load_env
from ..slack import api


def _die(msg: str, code: int = 1) -> None:
    print(f"[slack] {msg}")
    sys.exit(code)


def _parse(argv: list[str]) -> tuple[str, dict]:
    if not argv:
        print(__doc__)
        sys.exit(1)
    cmd, rest = argv[0], argv[1:]
    opts: dict = {"file": [], "limit": "50"}
    i = 0
    while i < len(rest):
        arg = rest[i]
        if not arg.startswith("--"):
            _die(f"unexpected argument {arg!r}")
        key, _, inline = arg.partition("=")
        field = key[2:].replace("-", "_")
        if inline:
            value = inline
            i += 1
        elif i + 1 < len(rest) and not rest[i + 1].startswith("--"):
            value = rest[i + 1]
            i += 2
        else:
            _die(f"{key} needs a value")
        if field == "file":
            opts["file"].append(value)
        else:
            opts[field] = value
    return cmd, opts


def _read_text(path_str: str, label: str) -> str:
    path = Path(path_str)
    if not path.exists():
        _die(f"{label} not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        _die(f"{label} is empty: {path}")
    return text


def cmd_post(opts: dict) -> None:
    channel = opts.get("channel") or _die("post needs --channel")
    if not opts.get("text_file"):
        _die("post needs --text-file (message text is never passed as a shell argument)")
    text = _read_text(opts["text_file"], "--text-file")

    try:
        ts = api.post_message_checked(channel, opts.get("thread"), text)
    except api.SlackError as exc:
        _die(f"post failed: {exc}", 2)

    if api.message_exists(channel, ts):
        print(f"[slack] posted and verified: channel={channel} ts={ts} ({len(text)} chars)")
    else:
        # Delivered per the API but not readable back — say so rather than
        # letting the agent tell the user it landed.
        print(f"[slack] WARNING: posted (ts={ts}) but could not read it back — verify manually")
        sys.exit(2)


def cmd_upload(opts: dict) -> None:
    channel = opts.get("channel") or _die("upload needs --channel")
    files = opts.get("file") or []
    if not files:
        _die("upload needs at least one --file")
    comment = _read_text(opts["comment_file"], "--comment-file") if opts.get("comment_file") else None

    uploaded, failed = [], []
    for n, f in enumerate(files):
        path = Path(f)
        if not path.exists():
            failed.append(f"{f}: not found")
            continue
        try:
            file_id = api.upload_file(
                channel,
                opts.get("thread"),
                path,
                title=opts.get("title") if len(files) == 1 else path.name,
                # Only the first upload carries the comment, else it repeats.
                comment=comment if n == 0 else None,
            )
        except api.SlackError as exc:
            failed.append(f"{path.name}: {exc}")
            continue
        if api.file_is_shared(file_id, channel):
            uploaded.append(f"{path.name} (id={file_id})")
        else:
            failed.append(f"{path.name}: uploaded but not visible in {channel}")

    for u in uploaded:
        print(f"[slack] uploaded and verified: {u}")
    for f in failed:
        print(f"[slack] FAILED: {f}")
    if failed:
        sys.exit(2)


def cmd_read_thread(opts: dict) -> None:
    channel = opts.get("channel") or _die("read-thread needs --channel")
    thread = opts.get("thread") or _die("read-thread needs --thread")
    try:
        body = api.api_get("conversations.replies", {
            "channel": channel, "ts": thread, "limit": opts["limit"],
        })
    except api.SlackError as exc:
        _die(f"read-thread failed: {exc}", 2)

    messages = body.get("messages", [])
    print(f"[slack] {len(messages)} message(s) in thread {thread}")
    for m in messages:
        who = "bot" if m.get("bot_id") else m.get("user", "?")
        text = api.unwrap_slack_text(m.get("text", "")).replace("\n", " ")
        line = f"  [{m.get('ts')}] {who}: {text[:300]}"
        names = [f.get("name", "?") for f in m.get("files", []) or []]
        if names:
            line += f"   FILES: {', '.join(names)}"
        print(line)


def cmd_channels(opts: dict) -> None:
    try:
        channels, warning = api.list_channels()
    except api.SlackError as exc:
        _die(f"channels failed: {exc}", 2)
    needle = (opts.get("filter") or "").lower()
    rows = [c for c in channels if needle in c["name"].lower()]
    rows.sort(key=lambda c: (not c["is_member"], c["name"]))
    if warning:
        print(f"[slack] NOTE: {warning}")
    print(f"[slack] {len(rows)} channel(s); the bot can only post where is_member=True")
    for c in rows:
        mark = "member" if c["is_member"] else "  --  "
        kind = "private" if c["is_private"] else "public "
        print(f"  {mark}  {kind}  #{c['name']}  ({c['id']})")


def cmd_search(opts: dict) -> None:
    query = opts.get("query") or _die('search needs --query "<text>"')
    try:
        hits = api.search_history(query)
    except api.SlackError as exc:
        _die(f"search failed: {exc}", 2)
    limit = int(opts.get("limit") or 20)
    print(f"[slack] {len(hits)} message(s) containing {query!r} "
          "(channels the bot is in; Slack's workspace search needs a user token)")
    for h in hits[:limit]:
        text = " ".join(h["text"].split())
        print(f"  #{h['channel']} [{h['ts']}] {h['user']}: {text[:220]}")
        if h.get("files"):
            print(f"      ATTACHMENTS ({len(h['files'])}): {', '.join(h['files'])}")
        for n in h.get("nearby", []):
            tag = f" [+{len(n['files'])} file(s): {', '.join(n['files'])}]" if n["files"] else ""
            print(f"      · nearby {n['user']}: {n['text']}{tag}")
    if hits:
        # The failure this guards against: a claim and the screenshots supporting
        # it are separate messages, so the matched line alone reads as settled fact.
        print("[slack] a matched line is not the whole story — check the nearby messages, "
              "and for any ATTACHMENTS run:  fetch-files --channel <id> --ts <ts>  and read "
              "them before concluding anything from the text")


def cmd_fetch_files(opts: dict) -> None:
    channel = opts.get("channel") or _die("fetch-files needs --channel")
    ts = opts.get("ts") or _die("fetch-files needs --ts (from a search or read-thread line)")

    # Default to the OS temp dir, not the project tree. Slack attachments are
    # looked at once and then irrelevant, and the project tree lives inside
    # OneDrive, which syncs every byte regardless of .gitignore. --out persists
    # them deliberately when a figure is worth keeping.
    if opts.get("out"):
        out = Path(opts["out"])
    else:
        out = Path(tempfile.gettempdir()) / "lore_slack_files" / ts.replace(".", "_")
    try:
        saved = api.fetch_message_files(channel, ts, out)
    except api.SlackError as exc:
        _die(f"fetch-files failed: {exc}", 2)
    # Bound the cache: these are re-fetchable in seconds, so old ones go.
    from ..cache_prune import prune_quietly
    prune_quietly(out.parent, max_age_days=1, max_mb=50)

    if not saved:
        print(f"[slack] no downloadable files on message {ts}")
        return
    where = "temp (auto-pruned after a day)" if not opts.get("out") else str(out)
    print(f"[slack] {len(saved)} file(s) downloaded to {where} — READ THEM before "
          "drawing any conclusion from the message text:")
    for p in saved:
        print(f"  {p}")


COMMANDS = {
    "post": cmd_post,
    "upload": cmd_upload,
    "read-thread": cmd_read_thread,
    "channels": cmd_channels,
    "search": cmd_search,
    "fetch-files": cmd_fetch_files,
}


def main() -> None:
    load_env()
    cmd, opts = _parse(sys.argv[1:])
    handler = COMMANDS.get(cmd)
    if handler is None:
        _die(f"unknown command {cmd!r} — one of: {', '.join(COMMANDS)}")
    handler(opts)


if __name__ == "__main__":
    main()
