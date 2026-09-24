# Deploying to the lab machine

The lab machine is production; this laptop is where development and testing
happen. Nothing below should run until the live test in `docs/live_test_plan.md`
has passed on the laptop.

## Order matters

Socket Mode **load-balances events across every connected listener**. If both
machines' listeners are up, roughly half your Slack messages go to each — and
during a rollout that means half hit old code. Hours were lost to exactly this
once: a "missing" session log was simply on the other computer.

So: **one listener at a time, always.**

## Steps

### 1. Stop the lab machine's listener

On the lab machine, before anything else. Confirm it is actually gone — a stale
process with old code is the failure mode this whole ordering exists to avoid.

### 2. Get the code

```bash
cd <lab machine LORE checkout>
git fetch origin
git checkout main
git pull
```

`CLAUDE.md` and `.claude/skills/` must arrive together — the router depends on
the skills, and a router with nothing to invoke is a dead pipeline. They are in
the same commits, so a normal pull is fine; just do not cherry-pick.

### 3. Install the RAG extras

```bash
# in the env the pipeline actually runs in (presto), not base
pip install -e '.[rag]'
```

Without this the knowledge graph silently no-ops and Q&A falls back to keyword
search. `lightrag-hku`, `sentence-transformers` and `torch` are the heavy ones.

### 4. Point the knowledge base outside any synced folder

```bash
# in .env on the lab machine
KB_STORAGE_DIR=C:/Users/<user>/AppData/Local/lore_kb
```

**Not** inside OneDrive/Dropbox. Sync locks break LightRAG's atomic
`.tmp -> rename` writes with `WinError 5`, which previously failed most pages of
a build. Consider excluding `outputs/` from sync too — it was 243 MB before the
retention work.

### 5. Build the lab machine's own corpus and graph

```bash
python -m lab_agent.cli.build_kb --index
```

`knowledge/` and the KB are gitignored, so they do **not** arrive with the pull —
this machine builds its own. Expect this to take a while on the first run and to
consume plan usage; it is resumable and checkpointed, so if it stops on a usage
limit just run it again.

### 6. Verify before starting the listener

```bash
python -c "from lab_agent.config import check_env; check_env(live=True)"
python -m lab_agent.cli.eval_qa
for t in tests/test_*.py; do PYTHONPATH=. python "$t"; done
```

All four live checks valid, `eval_qa` 6/6, 24 test files passing. If the Claude
CLI login has expired here, `claude -p` fails and the graph returns nothing —
re-authenticate with `claude` before relying on Q&A.

### 7. Start the listener

```bash
python slack_listener.py
```

The startup line mentioning the nightly KG refresh confirms new code is loaded.
**Restart it after any change to `sessions.py` or `listener.py`** — CLAUDE.md and
the skills are re-read per session, but the spawn prompt is baked into the
running process.

### 8. Smoke test on the real path

Send one of each from Slack and confirm the reply:

- a question naming a page → answered from the corpus, fast, no cookie
- a photo with no caption → the bot wakes (this is new)
- `how's the DR right now?` → a short status, no upload

Only then hand it back to the lab.

## Rollback

`main` before this release is `8fa677a`. `git checkout 8fa677a` on the lab
machine restores the previous behaviour; the KB and corpus are unaffected because
they live outside the repo.

## Optional: let another agent query LORE

To give a second Claude Code session, such as a measurement agent on the DAQ machine,
read-only access to LORE's knowledge over SSH, follow `docs/mcp_ssh_setup.md` after the
steps above. It adds no listening port and changes nothing about how LORE itself runs.
