# claude-manager

Persistent state for your projects, shared between **Claude Code** (through
MCP and hooks) and **you** (through a web interface), plus an agent queue
that works while you do something else.

The problem it solves is not "tracking tasks": it is **starting from scratch
every session**. Claude re-reads the code, rediscovers past decisions, forgets
what was planned. Here, a `SessionStart` hook injects the full project state
as soon as the session opens: progress, tasks by priority, decisions, pitfalls
already hit, stack, commands, services, log.

> **You are an AI and someone handed you this repository to install?** Go
> straight to [Installation](#installation). Every step says what to do, how
> to verify it, and what requires a human. Read
> [Rules for an AI doing the install](#rules-for-an-ai-doing-the-install) first.

The web interface and the agent prompts are in French.

---

## What it does

| Area | Content |
|---|---|
| **Projects** | slug, path on disk, repository, status, computed progress |
| **Tasks** | priority, status, owner, tags, subtasks, checkboxes, manual tests, reminders |
| **Memory** | decisions, conventions, pitfalls, **mistakes not to repeat** — injected into every briefing |
| **Stack** | technologies, versions, role, abandoned options and why |
| **Log** | timestamped record of what was done |
| **Agent queue** | a queued task is handled by `claude -p` in a dedicated worktree, tested, committed on a branch; you review, then merge |
| **Lessons learned** | an agent that fails, or whose work you reject, leaves a "mistake" memory for the next ones |
| **Issue reports** | outside users describe a problem to an AI (which reads the code, read-only); nothing becomes a task without your approval |
| **Notifications** | Telegram: agent question, failing tests, new issue report, reminder due… |
| **Search** | full text over everything (SQLite FTS5) |

## Architecture

```
                         ┌──────────── Docker container ─────────────┐
 Claude Code ── MCP ───► │ FastAPI: /mcp/  /api/  web interface      │
 (hooks)    ── HTTP ───► │          /support (issue reporters)       │
 browser ──────────────► │          /mcp-support/ (support AI)       │
                         │       SQLite (WAL + FTS5) in ./data       │
                         └───────────────────▲───────────────────────┘
                                             │ HTTP (token)
                         ┌───────────────────┴───────────────────────┐
                         │ host daemon: worker/agent_worker.py       │
                         │  runs `claude -p` (agents, lessons,       │
                         │  support AI), git, tests, merges          │
                         └───────────────────────────────────────────┘
```

The container cannot run anything on the machine: the **daemon**, on the
host, under the account that has Claude Code, runs the agents.

- `app/repo.py` — **all** SQL queries; the only write path
- `app/briefing.py` — builds the briefing
- `app/mcp_server.py` — Claude's MCP tools
- `app/support_mcp.py` — read-only MCP for the issue-report AI
- `app/web/` — interface (no external JavaScript dependency)
- `worker/agent_worker.py` — the daemon
- `hooks/` — Claude Code hooks (`SessionStart`: briefing; `Stop`: reminder to record work)
- `skills/suivi-projet/` — Claude Code skill: what to record, and where

---

## Installation

### Prerequisites

| Tool | Why | Check |
|---|---|---|
| Linux | the host | — |
| Docker + Compose v2 | the server | `docker compose version` |
| Python ≥ 3.10 on the host | hooks and daemon (standard library only) | `python3 --version` |
| git | daemon, worktrees | `git --version` |
| Claude Code, **logged in** | the daemon runs `claude -p` | `claude --version`, then `claude -p "say ok"` |
| Traefik (optional) | public HTTPS exposure | — |

The user running the daemon must be able to use Docker (`docker ps` without
sudo) and have Claude Code logged in.

### 1. Clone

Clone **inside the folder that holds your projects** (the future
`CM_PROJECTS_ROOT`): the container reads the agent logs through that mount.

```bash
cd ~/projects            # your projects folder
git clone git@github.com:kpidoff10/claude-manager.git
cd claude-manager
mkdir -p data logs/runs
```

Elsewhere works too, as long as `CM_RUN_LOG_DIR` (step 2) points to a path the
container can see.

### 2. Configure `.env`

```bash
cp .env.example .env
python3 - <<'PY'
import secrets, re, pathlib
p = pathlib.Path(".env"); s = p.read_text()
for key in ("CM_API_TOKEN", "CM_SESSION_SECRET"):
    s = re.sub(rf"^{key}=.*$", f"{key}={secrets.token_urlsafe(32)}", s, flags=re.M)
# Web password without ambiguous characters (I/l, O/0).
alpha = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
pwd = "".join(secrets.choice(alpha) for _ in range(20))
s = re.sub(r"^CM_WEB_PASSWORD=.*$", f"CM_WEB_PASSWORD={pwd}", s, flags=re.M)
p.write_text(s)
PY
chmod 600 .env
```

Then fill in by hand:

| Variable | Value |
|---|---|
| `CM_PUBLIC_URL` | the public address, e.g. `https://manager.example.com` — or `http://localhost:8099` without exposure |
| `CM_DOMAIN` | the bare domain, for Traefik (`manager.example.com`); `localhost` without Traefik |
| `CM_PROJECTS_ROOT` | the folder holding your projects, e.g. `/home/me/projects`. Mounted **at the same path** in the container, read-only |
| `CM_PREVIEW_HOST` | IP or hostname of the machine, for the agents' preview links |
| `CM_TELEGRAM_TOKEN` | optional (see [Telegram](#telegram-optional)) |
| `CM_RUN_LOG_DIR` | absolute path of this repository's `logs/runs`, e.g. `/home/me/projects/claude-manager/logs/runs` |

Optional: `CM_MAX_PARALLEL` (concurrent agents, 3), `CM_AGENT_TIMEOUT`
(seconds per agent, 2700), `CM_MAX_RETRIES` (3), `CM_TZ` (`Europe/Paris`),
`CM_WORKTREES` (agents' working copies, `~/worktrees` — **outside** the
projects folder, so a copy is never mistaken for a project).

### 3. Start the server

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8099/health      # → {"status":"ok",...}
```

The server listens on `127.0.0.1:8099` (hooks and daemon). With Traefik, the
labels in `docker-compose.yml` expose it over HTTPS on `CM_DOMAIN`; the
container must then share a network with Traefik (add that network to the
service if your Traefik does not use the default one).

**The code is copied into the image**: after any change to `app/`, run
`docker compose up -d --build`. A plain `up -d` is only enough for `.env`.

### 4. Connect Claude Code

**MCP** (user scope: available in every project):

```bash
TOKEN=$(grep ^CM_API_TOKEN= .env | cut -d= -f2)
claude mcp add --scope user --transport http claude-manager \
  "http://127.0.0.1:8099/mcp/" --header "Authorization: Bearer $TOKEN"
claude mcp list          # claude-manager should show as connected
```

The trailing slash of `/mcp/` is required. From another machine than the
server, use `CM_PUBLIC_URL` + `/mcp/`.

**Hooks** — **merge** into `~/.claude/settings.json` (never overwrite the
file: it holds other settings). Replace `PATH_TO_REPO` with the absolute path
of this repository:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "python3",
        "args": ["PATH_TO_REPO/hooks/session_start.py"], "timeout": 10,
        "statusMessage": "Loading project state…" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "python3",
        "args": ["PATH_TO_REPO/hooks/stop_check.py"], "timeout": 20,
        "statusMessage": "Checking recorded state…" } ] }
    ]
  }
}
```

If `SessionStart` or `Stop` already exist, **append** an entry to their list
instead of replacing them.

**Skill**:

```bash
mkdir -p ~/.claude/skills
cp -r skills/suivi-projet ~/.claude/skills/
```

### 5. Run the daemon (and keep it running across reboots)

With systemd (recommended):

```bash
mkdir -p ~/.config/systemd/user
sed "s|__CHEMIN__|$PWD|g" deploy/claude-manager-worker.service \
  > ~/.config/systemd/user/claude-manager-worker.service
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now claude-manager-worker
tail -f logs/worker.log        # → « démon de file démarré »
```

Without a systemd user session (container, no D-Bus), start at boot via cron:

```bash
( crontab -l 2>/dev/null; echo "@reboot cd $PWD && python3 -u worker/agent_worker.py >> logs/worker.log 2>&1" ) | crontab -
cd "$PWD" && setsid nohup python3 -u worker/agent_worker.py >> logs/worker.log 2>&1 < /dev/null &
```

**Only one daemon at a time.** To stop it: `systemctl --user stop
claude-manager-worker`, or `kill <pid>` — beware, `pkill -f agent_worker` also
kills the shell running the command, since its own command line contains the
pattern.

### 6. Verify

```bash
curl -s http://127.0.0.1:8099/health
TOKEN=$(grep ^CM_API_TOKEN= .env | cut -d= -f2)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8099/api/queue/state
python3 scripts/mcp_call.py list_projects '{}'
grep "démon de file démarré" logs/worker.log | tail -1
```

Then open `CM_PUBLIC_URL` in a browser, log in with `CM_WEB_PASSWORD`, and
open a Claude Code session in a registered project: the briefing should show
up at startup.

**Register a first project**: from a Claude Code session in the project's
folder, ask "register this project in claude-manager and scan its stack"
(tools `upsert_project`, then `scan_stack`).

---

## Rules for an AI doing the install

- **Never print or copy** the values in `.env` (token, password, secret) into a
  reply, a commit or a log. Give the web password to the human once, at the
  end, telling them to write it down.
- **Never overwrite** `~/.claude/settings.json` or `~/.claude.json`: merge.
  Make a `.bak` copy before touching them.
- `.env`, `data/` and `logs/` **never** go into git (already in `.gitignore`).
- Ask the human, don't guess: the public domain, the projects folder, whether
  there is a Traefik. Everything else has a default.
- What requires a human, to point out at the end of the install:
  1. DNS for the domain (if publicly exposed);
  2. the Telegram bot (optional);
  3. GitHub deploy keys for the issue-report AI (optional).
- Verify each step with its check command before moving to the next one.

---

## Telegram (optional)

1. On Telegram, message **@BotFather**, `/newbot` → a token `1234:AA…`.
2. Put it in `CM_TELEGRAM_TOKEN`, then `docker compose up -d`.
3. **Send the bot a first message** (until you do, it is not allowed to write to you).
4. Web interface › Notifications: paste the chat ID, connect the webhook, tick the events.

## Issue reports (optional)

Outside users (e.g. branch offices) file tickets by chatting with an AI on
`/support`. You manage everything from **Signalements** in the interface:

- **Accounts**: name, login, allowed projects; generated password, shown once.
- **Support sheet** per project: what the AI knows about the software, written for an end user. And, so it can read the code, the SSH repository and branch.
- **Approval**: every submitted ticket waits for your decision. Approving creates a task (queued or not); rejecting requires a reason, visible to the user.
- **Screenshots**: users attach or paste (Ctrl+V) screenshots in the chat; the AI looks at them and can ask for one. On the support sheet you can upload **reference screenshots** with a caption, which the AI can show to users ("the button is here"). Your sessions and agents read a ticket's screenshots through the `get_signalement_capture` MCP tool. Only PNG, JPEG, GIF and WebP are accepted, detected from the file content (no SVG), 8 MB max.

**Reading the code**: the daemon keeps a copy of the repository in
`~/.cache/claude-manager/support-code/` with a dedicated key, **without write
access**:

```bash
ssh-keygen -t ed25519 -N "" -C "claude-manager support (read-only)" -f ~/.ssh/cm_support_deploy
cat ~/.ssh/cm_support_deploy.pub
```

Add it on GitHub › repository › Settings › Deploy keys, **without** "Allow
write access". A deploy key only works for one repository: for several, use a
read-only machine account instead.

**What the issue-report AI can do**: read the code copy (`Read`, `Grep`,
`Glob`; secret files are denied) and call the `/mcp-support/` MCP (support
sheet, known reports). Nothing else: no writes, no commands, no claude-manager
MCP, no project memory. Its only power is to propose a ticket text.

## Security

- `/mcp/` and `/api/`: bearer token (`CM_API_TOKEN`); web interface: password
  (`CM_WEB_PASSWORD`) and signed cookie.
- `/support`: reporter accounts, separate session (different cookie, different
  salt) that opens neither the interface, the API nor the MCP.
- `/mcp-support/`: HMAC token specific to each report, derived from
  `CM_SESSION_SECRET`; it only sees that report's project.
- The `env_vars` table only holds variable **names**, never values.
- Changing `CM_SESSION_SECRET` logs everyone out and invalidates the Telegram
  webhook (reconnect it from Notifications).

## Operations

```bash
docker compose up -d --build            # after a code change
docker logs -f claude-manager           # server log
tail -f logs/worker.log                 # daemon log
python3 scripts/mcp_call.py get_briefing '{"project":"my-project"}'
```

The database is `data/manager.db` (SQLite): it is the only state to back up.
