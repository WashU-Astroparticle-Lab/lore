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

**What it can do:** `status`, `resolve`, `read_page`, `search`, `ask_graph`. All read-only.
**What it cannot do:** write, upload, post to Slack, fetch from LabArchives, rebuild the
graph, or read any credential. `tests/test_mcp_server.py` enforces all of that.

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
PYTHON -m pip install --dry-run "mcp>=1.26" python-dotenv
```

Read the "Would install" line. If it would change the version of anything the
measurement code uses (numpy, scipy, the presto package, pydantic if your code uses it),
**stop and use the separate environment below.** If it only adds new packages, install:

```bash
PYTHON -m pip install "mcp>=1.26" python-dotenv
```

<details><summary>Separate environment instead (nothing shared with the lab's code)</summary>

The server needs only the MCP library and python-dotenv, not LORE's heavy packages:

```bash
conda create -n lore-mcp python=3.11 -y
conda activate lore-mcp
pip install "mcp>=1.26" python-dotenv
```

Then use this environment's python wherever these steps say `PYTHON` for the MCP server
(steps 4 and 6). The listener keeps running in LORE's usual environment.
</details>

### 3. Move the nightly refresh out of the night

Add to `.env`:

```
KB_REFRESH_HOUR=12
```

The refresh defaults to 2 a.m., which would land mid-run and pause graph answers for
about 40 s while the graph reloads. At noon, a person is around if it fails.

Now restart the listener, so it picks this up and the graph service is running.

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

Expected: **OK: server 'lore' answered**, with the five tools and the page count. **This
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
