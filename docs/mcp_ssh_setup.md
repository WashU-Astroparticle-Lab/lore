# LORE's MCP server: setup and debugging

Lets another Claude Code session, such as the measurement agent on the DAQ machine,
ask LORE questions. It connects over SSH, so no new network port opens and LORE's
graph service stays bound to `127.0.0.1`.

```
agent's machine                                    LORE's machine
Claude Code ── ssh (key can only start LORE's tools) ──► lore_mcp.cmd
                                                          └► python -m lab_agent.mcp_server
                                                               ├ reads the notebook corpus
                                                               └ asks the warm graph service
```

**What it can do:** `status`, `resolve`, `read_page`, `search`, `ask_graph`, and `dr_status`
(the fridge's thermometry, read from its own log; needs `DR_DATA_PATH` in `.env`), all
read-only; and `publish_notes` / `notes_status`, which file the agent's own notes as a new,
unreviewed page in the AI Agent folder (see "The agent's notes" below).
**What it cannot do:** edit or add to any existing page, upload anything else, post to
Slack, fetch from LabArchives, rebuild the graph, or read any credential. The server itself
never uploads: it queues notes, and LORE's listener publishes them.
`tests/test_mcp_server.py` enforces all of that.

Throughout, `PYTHON` means the `PYTHON` value in `lab_config.md` on that machine,
`LABUSER@LABHOST` means the lab machine's login and address (step 5), and `LORE\` means
the project folder.

---

## At the lab machine

If LORE is not on the lab machine yet, do `docs/deploy_lab_machine.md` first, then
come back here. That also gets the listener running, which the graph tool needs.

### 1. Update LORE

Stop the listener if it is running (Ctrl+C in its window), then:

```bash
cd LORE
git fetch origin
git checkout mcp-server
git pull
```

### 2. Install the MCP library, carefully

The environment LORE runs in may also run the lab's Presto code. Look before installing:

```bash
PYTHON -m pip install --dry-run "mcp>=1.26,<2" python-dotenv
```

Read the "Would install" line. If it would change the version of anything the
measurement code uses (numpy, scipy, the presto package, pydantic if your code uses it),
**stop and use the separate environment below.** If it only adds new packages, install:

```bash
PYTHON -m pip install "mcp>=1.26,<2" python-dotenv
```

<details><summary>Separate environment instead (nothing shared with the lab's code)</summary>

The server needs only the MCP library and python-dotenv, not LORE's heavy packages:

```bash
conda create -n lore-mcp python=3.11 -y
conda activate lore-mcp
pip install "mcp>=1.26,<2" python-dotenv
```

Then use this environment's python wherever these steps say `PYTHON` for the MCP server
(steps 4 and 6). The listener keeps running in LORE's usual environment.
</details>

**If `PYTHON` is the Microsoft Store Python** (its path contains `WindowsApps`), the MCP
server cannot use it, and `--write-launcher` refuses it. Under an SSH logon Windows often
refuses to start a Store app ("Unable to create process ... Access is denied"), and the
Store Python silently redirects writes under `AppData\Local` into its own package folder,
where sshd cannot see the launcher. A venv made from it inherits both problems. Use a
python.org Python for the server: the NuGet package needs no installer and no admin.

```powershell
Invoke-WebRequest https://api.nuget.org/v3-flatcontainer/python/3.13.15/python.3.13.15.nupkg -OutFile py.zip
Expand-Archive py.zip py_pkg; Move-Item py_pkg\tools C:\lore_mcp\py313; Remove-Item py.zip, py_pkg -Recurse
C:\lore_mcp\py313\python.exe -m pip install "mcp>=1.26,<2" python-dotenv
```

Then `C:\lore_mcp\py313\python.exe` is `PYTHON` for steps 4 and 6. The listener can stay on
the Store Python. The same redirection applies to anything else the listener writes under
`AppData\Local`, so keep `KB_STORAGE_DIR` out of it too (e.g. `C:/lore_kb`): otherwise the
listener's nightly refresh updates a private copy and the server keeps reading the old one.

### 3. Move the nightly refresh out of the night

Add to `.env`:

```
KB_REFRESH_HOUR=12
```

The refresh defaults to 2 a.m., which would land mid-run and pause graph answers for
about 40 s while the graph reloads. At noon, a person is around if it fails.

Now restart the listener, so it picks this up and the graph service is running. Its
startup line should read `Nightly KG refresh scheduled for 12:00`. (Before the fix of
2026-09-25 the listener read this setting before loading `.env`, so it was ignored and
the refresh ran at 2 a.m. whatever it said; if the line says `02:00`, pull.)

### 4. Check it works on this machine

```bash
PYTHON -m lab_agent.mcp_server --selftest
```

It should end with **All checks passed**, and show `graph_service: up` if the listener
is running. Add `--graph` to also try one real graph answer, which takes a minute.

### 5. Turn on the SSH server

In **PowerShell run as Administrator**:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
Get-NetFirewallRule -Name *OpenSSH-Server* | Select-Object Name, Enabled
```

If no firewall rule is listed:

```powershell
New-NetFirewallRule -Name sshd -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

If `Add-WindowsCapability` is refused, the machine is centrally managed; that needs IT.

Write down the login and address:

```powershell
whoami      # COMPUTERNAME\user for a local account, DOMAIN\user for a domain one
hostname
ipconfig    # the IPv4 address
```

For a local account, the SSH login is `user@HOSTNAME`. For a domain account it is
`user@DOMAIN@HOSTNAME`, using the domain `whoami` printed. The IPv4 address works in
place of the host name.

### 6. Write the launcher

```bash
PYTHON -m lab_agent.mcp_server --write-launcher
```

It writes `lore_mcp.cmd` and prints two things you need in step 9: the exact
`restrict,command="..."` line, and **which file** the keys go in. Leave the window open.

---

## On your laptop

### 7. Make two keys

In PowerShell:

```powershell
ssh-keygen -t ed25519 -f $HOME\.ssh\lore_lab -C "lore-lab-debug"
ssh-keygen -t ed25519 -f $HOME\.ssh\lore_mcp -C "lore-mcp" -N '""'
```

- `lore_lab` is **your** key, for a normal shell while debugging. Give it a passphrase.
- `lore_mcp` is **Claude Code's** key. It has no passphrase because Claude Code starts SSH
  in the background and cannot type one. That is acceptable because this key will only
  ever be able to start LORE's read-only tools.

### 8. Send the public halves to the lab machine

Copy `lore_lab.pub` and `lore_mcp.pub` (Slack, email or USB; public halves are safe to
share). **Never** copy the files without `.pub`: those are the private keys.

---

## Back at the lab machine

### 9. Install the keys

Step 6 told you which file. It is one of:

- **Administrator account** (most common): `C:\ProgramData\ssh\administrators_authorized_keys`
- **Standard account:** `C:\Users\<you>\.ssh\authorized_keys`

Put two lines in it: the debug key as-is, and the MCP key after the prefix from step 6.
Easiest is to let step 6 build the MCP line for you:

```bash
PYTHON -m lab_agent.mcp_server --write-launcher path\to\lore_mcp.pub
```

The file then holds:

```
ssh-ed25519 AAAA...(contents of lore_lab.pub)... lore-lab-debug
restrict,command="C:\Users\...\lore_mcp\lore_mcp.cmd" ssh-ed25519 AAAA...(contents of lore_mcp.pub)... lore-mcp
```

Edit it in **Notepad**, run as Administrator for the ProgramData file. Do not use
PowerShell's `>` or `echo`: Windows PowerShell writes UTF-16, and sshd silently ignores
a UTF-16 keys file.

**Administrator file only:** lock it down, or sshd ignores it:

```powershell
icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

---

## Test from your laptop, one layer at a time

You need to be on the campus network or VPN. Each step only runs once the previous one
works, so a failure points at exactly one layer.

### 10. Plain SSH, with your debug key

```powershell
ssh -i $HOME\.ssh\lore_lab LABUSER@LABHOST
```

Answer `yes` to the host-key question the first time. You should get a prompt on the
lab machine; `exit` to leave. **This proves the network and the SSH server work.**

If the key is refused, try `ssh LABUSER@LABHOST` with the account's password. If that
works, the network is fine and the problem is only the keys file (step 9).

### 11. The MCP key, probed

From the LORE folder on your laptop, using the full interpreter path (bare `python` on
your laptop is a different install without LORE's packages):

```powershell
C:\Users\axelr\miniconda3\python.exe -m lab_agent.mcp_server --probe ssh -i C:/Users/axelr/.ssh/lore_mcp -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new LABUSER@LABHOST
```

Expected: **OK: server 'lore' answered**, with the eight tools and the page count. **This
proves the forced command, the launcher and the protocol all work through SSH**, using the
exact exchange Claude Code will perform.

### 12. Claude Code

Use a **separate folder**, not the LORE folder: a server added in the LORE folder would
be loaded by every Claude session there, including ones LORE's own listener spawns if it
runs on your laptop.

```powershell
mkdir $HOME\lore-mcp-test
cd $HOME\lore-mcp-test
claude mcp add lore -- ssh -i C:/Users/axelr/.ssh/lore_mcp -T -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 LABUSER@LABHOST
claude
```

In that session, `/mcp` should show `lore` as connected. Then try:

1. *"Use LORE's status tool."*
2. *"What is the mean Qc measured on the 9 devices in BE260416?"*

The second should go `resolve` → `read_page`, and find `20260702_JPL_QPDs`.
`ServerAliveInterval` keeps an idle connection from being dropped during a long night.

### Other MCP clients (Cursor, the OpenAI Agents SDK, Codex)

Nothing on the lab machine changes; the agent's client just runs the same `ssh` command.
Two things to carry over from step 12:

- **Set `PROGRAMDATA` in the server's environment.** Windows' `ssh.exe` exits 255 without
  printing anything when `PROGRAMDATA` is missing, and some clients start servers with a
  stripped-down environment (the MCP Python SDK's default does, which the OpenAI Agents
  SDK uses). The only symptom is "connection closed".
- **A login name with a space** (e.g. a Windows account `lc control`) goes in `-l`, as its
  own argument, rather than in `user@host`.

Cursor, in `%USERPROFILE%\.cursor\mcp.json` on the agent's machine:

```json
{
  "mcpServers": {
    "lore": {
      "command": "C:\\Windows\\System32\\OpenSSH\\ssh.exe",
      "args": [
        "-i", "C:\\Users\\<you>\\.ssh\\lore_mcp",
        "-l", "LABUSER",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "LABHOST"
      ],
      "env": { "PROGRAMDATA": "C:\\ProgramData" }
    }
  }
}
```

Before opening the client, run the same command by hand from the agent's machine. If it
connects and then sits silently, that is LORE's server waiting for MCP messages: it works,
and Ctrl+C ends it. On the lab machine, `Get-WinEvent -LogName OpenSSH/Operational
-MaxEvents 10` (no admin needed) shows each `Accepted publickey` with the key's fingerprint.

### What the agent asked

Every tool call is appended to `session_logs\mcp_calls.jsonl` on LORE's machine
(gitignored; set `LORE_MCP_LOG` to put it elsewhere): the time, the tool, its arguments,
the calling machine's address, how long it took, and the full result the agent received.
Calls from one SSH connection share a `session` id. It is the only file the server
writes, and a failure to write it never fails the call. To read a night's questions:

```powershell
Get-Content session_logs\mcp_calls.jsonl | ConvertFrom-Json |
  Select-Object at, caller, tool, @{n='args'; e={$_.args | ConvertTo-Json -Compress}}
```

### The agent's notes

Two ways back, and the agent should use both at the end of a run:

**Into the notebook, directly: `publish_notes(title, markdown, run)`.** The server writes
the note to `agent_inbox\pending\` on LORE's machine (gitignored; `LORE_AGENT_INBOX`
overrides). The Slack listener, which holds the LabArchives keys, checks that inbox every
30 s and creates a **new page in the AI Agent folder** titled
`[UNSIGNED] Agent notes <date time> — <title>`, headed by a banner saying the measurement
agent wrote it from which machine and that nobody reviewed it, with the markdown attached.
The agent calls `notes_status(note_id)` to confirm the page landed.

- It only ever creates new pages, only in the upload folder. Raw HTML in the notes is shown
  as text. Resubmitting the same notes does not make a second page. At most 20 notes wait
  at once, and a note over 200,000 characters is refused.
- It needs the **listener running**. If it is down, notes wait in `pending\` and are
  published when it is back; `notes_status` says so.
- A failed upload is retried after 2, 4, 8 and 16 minutes, then moved to
  `agent_inbox\failed\` with the error, which `notes_status` reports.
- The listener window logs each one as `[agent-notes] note-… published: <page title>`.

**Into the repository, with the data.** The agent also commits the notes to an `Agent/`
folder inside the run folder (e.g. `analysis_archive/DAQ/PRIMA_JKID_JPLQPD_20260831/Agent/`)
or names them `agent*.md`. A later LORE report on that run writes every notes file in the
folder to `repo_notes.md`, headed by who wrote it; the GitHub analyst uses agent notes for
where to look, never as the source of a value, the rule LORE applies to its own
`[UNSIGNED]` drafts.

Put the convention in the agent's own instructions (its `.cursor/rules` or `CLAUDE.md`),
e.g. *"At the end of a run, or a phase of one, write your notes as markdown: say which
values you measured and which you inferred or read from LORE. File them with LORE's
publish_notes and confirm with notes_status, and commit the same file to the run's
`Agent/` folder with the data."*

---

## When something fails

| What you see | Where it broke | Fix |
|---|---|---|
| `Connection timed out` / `Could not resolve hostname` | network | On campus or VPN? Right host or IP? Firewall rule from step 5? |
| `Connection refused` | SSH server | `Get-Service sshd` on the lab machine; start it |
| `Permission denied (publickey)` | keys file | Admin account → must be the **ProgramData** file, with the `icacls` step. Saved as UTF-8, not UTF-16? |
| `Host key verification failed` | first connect | Do step 10 once by hand, or keep `StrictHostKeyChecking=accept-new` |
| Step 10 works, step 11 hangs, then times out | something prints to stdout | Is PowerShell the SSH default shell with a profile that prints? The default, cmd, is fine |
| `The system cannot find the path specified` | launcher path | Re-run `--write-launcher` on the lab machine; copy its line exactly |
| `status` works but `graph_service: not responding` | listener | Start the listener on the lab machine. The other tools do not need it |
| `/mcp` shows lore as failed, but step 11 passed | Claude Code config | `claude mcp get lore` to check the command matches step 11 exactly |
| `TCP connect ... failed` from the agent's machine, although it works on the lab machine | campus network | The two machines are on networks that cannot reach each other (e.g. different Wi-Fi networks). Put both on the same one, or use a wired link |
| `Unable to create process using "...python.exe" ... Access is denied` | launcher's Python | It is the Microsoft Store Python, or a venv made from it. Use a python.org Python (step 2) |
| Connection closes at once, ssh prints nothing, exit code 255 | client environment | `PROGRAMDATA` is not set for `ssh.exe`. Add it to the server's `env` in the client's config |
| `No module named 'mcp.server.fastmcp'` in the server's stderr | MCP library version | mcp 2.x is installed; `pip install "mcp>=1.26,<2"` |

For more detail from the server side, set `LogLevel DEBUG3` in
`C:\ProgramData\ssh\sshd_config`, restart sshd, and read
`C:\ProgramData\ssh\logs\sshd.log`. Set it back afterwards.

---

## Afterwards

- Once everything works, you can delete the `lore_lab` line from the keys file if you do
  not want a shell login kept open. The MCP key does not need it.
- Only one listener at a time: turn your laptop's off once the lab machine's is running,
  or Slack splits messages between them.
- For the measurement computer, repeat steps 7 to 12 on it, with its own `lore_mcp` key.
  Give each agent machine its own key so any one can be revoked alone. For a longer-term
  setup, a dedicated standard (non-admin) account on the lab machine for these keys is
  tidier than an administrator's.
