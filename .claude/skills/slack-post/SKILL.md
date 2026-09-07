---
name: slack-post
description: Draft, get approval for, and send a Slack message to a channel — with figures attached. Use when the user asks to "post this to the group", "send a summary to #channel", "share these plots", or wants to announce a result to the lab. Handles the draft → approve → send loop in as few turns as possible, and never posts to a channel without explicit approval.
---

# Posting to a Slack channel

Sending a short message with a couple of plots is a *small* task and must cost the user a small number of turns. One real exchange took **ten messages and 26 minutes** to send five lines and two figures — longer than writing it by hand. Everything below exists to stop that.

## The rule that never bends

**Never post to a channel until the user has approved the exact text and named the channel.** Posting into their DM thread so they can see it is fine and encouraged; posting to a shared channel is not, until they say so.

## Step 1 — gather everything BEFORE drafting

Do all of this first, so the draft arrives complete:

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.slack channels --filter <guess>   # which channels can I post to?
```

- **Offer the channel list with the draft.** Do not send a draft and then ask "which channel?" as a separate turn — that question was asked four times in one thread while the list was one command away. If the bot token lacks `groups:read`, the command still returns every **public** channel and prints a note saying private ones were skipped; use what it returns and ask only about a private channel.
- **Find the figures now**, in the experiment's `outputs/<id>/` directory, and name them in the draft.
- **Get the LabArchives link from `metadata.json`** (`labarchives_upload.notebook_url` plus folder/page) — never construct, shorten, or elide a URL. A fabricated GitHub link in one draft 404'd for the whole lab, and private repos 404 for anyone outside them anyway, so prefer the LabArchives location.

## Step 2 — one complete draft

Post the draft to the user (DM/thread), containing:

1. the exact message text you would send;
2. the figures you would attach, by filename;
3. the channel list, with your suggestion.

Then ask one question: *"Send this to #x, or tell me what to change?"*

**Style — match the lab, not a press release.** Short. Results first. Plain sentences, exact numbers with units, the same hedging as the source report. No headers, no bullet-point pyramids, no "Executive Summary". Three to five bullets is right. If the user has shown you an example of what good looks like, copy its register.

**Scope it.** If the work spans several runs, ask which one the message is about rather than covering everything — the answer is almost always "the latest".

## Step 2b — check the numbers before you show anyone

If the draft states results from an experiment, run it past its source:

```bash
cd $PROJECT_ROOT
$PYTHON -m lab_agent.cli.verify_claims --draft <draft file> --source outputs/<experiment_id>
```

It compares every number in the draft against the **report** for that run (not the raw
extractions — those hold tens of thousands of numbers and would pass anything). Exit 1
lists the values that appear in no source, with the line each came from.

**Know what it does and does not catch.** It catches invented, mistyped and half-remembered
numbers. It does **not** catch a real number attached to the wrong label — one real draft
said "+0.01 to +0.35 dBm at both tones" when the two tones were +0.01–0.15 and +0.13–0.35,
and every one of those numbers exists in the report. So still read the draft against the
report for pairing and hedging:

- is each number attached to the right tone / instrument / device?
- does an open question in the report get stated as settled in the draft? (that same draft
  gave the filter as VBF-7331+ while the report flags VBF-7331+ vs VBF-3321+ as unresolved)
- is anything hedged in the report stated flatly here?

## Step 3 — send, verified

Write the approved text to a file, then:

```bash
cd $PROJECT_ROOT
python -m lab_agent.cli.slack post   --channel <C…> --text-file <path>
python -m lab_agent.cli.slack upload --channel <C…> --file <fig1> --file <fig2>
```

- Message text goes in a **file**, never in a shell argument — backticks and quotes in a message have been executed by the shell, silently deleting words from what the user received.
- Both commands **verify by reading back** and exit non-zero if they cannot. **Only report success on exit 0.** Announcing "both plots are in your thread now" when nothing had been shared cost three extra round-trips in one real exchange.
- To attach figures to the message rather than trailing it, pass `--thread <ts of the posted message>`; `post` prints that ts.
- If an upload reports a failure, **check `read-thread` before reporting it** — the
  verification can lose a race with Slack's indexing. One run reported all three files
  "uploaded but not visible" while they were already in the thread, and the reply
  invented a permission problem to explain it. Say which file and why only once you
  have confirmed it really is absent.

## Step 4 — confirm precisely

Tell the user what went where: channel, message, and the filenames that attached. If the user asked you to omit something ("don't include that last part"), re-read your text and confirm it is gone before sending.
