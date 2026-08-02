# Full deployment runbook — Mattermost + agent-harness + mm-bridge

**Audience:** an AI agent (or engineer) standing up the complete stack from scratch on a
Linux host. Follow the steps in order. Each step ends with a **✅ Checkpoint** you must
verify before moving on. If a checkpoint fails, stop and fix it — do not proceed.

**What you are building:** a Mattermost chat server where each channel (or thread) is
bound to a coding-agent session. Humans talk to `@<bot>` in Mattermost; the bridge relays
turns to a local harness, which runs the real `claude` / `codex` / `pi` CLIs as
subprocesses on this host.

```
Mattermost (Docker)  ──WebSocket/REST──▶  mm-bridge  (host service)
   :8065, bot @<bot>                          │  HTTP + SSE
                                              ▼
                                       agent-harness  (host service, :8877)
                                              │  spawns as subprocesses
                                              ▼
                                   claude / codex / pi CLIs
                              (run as the host user, inside your repos)
```

## Topology — why this split

Install **Mattermost in a container** and the **two Python services on the host**. Do
**not** containerize agent-harness or mm-bridge:

- agent-harness spawns the coding-agent CLIs, which must run **as the host user** with
  access to that user's git identity, repositories on disk, CLI credentials
  (`~/.claude`, `~/.codex`), and PATH. Putting them in a container fights all of that.
- Mattermost is a self-contained stateful server — an ideal container.

| Component | Where | Runs as |
|---|---|---|
| Mattermost + Postgres | Docker (`mattermost/docker`) | container |
| agent-harness | Host, systemd **user** unit | the host user |
| mm-bridge | Host, systemd **user** unit | the host user |

---

## Decision tree — branch here first

Three choices shape the path through this runbook. Settle them with the operator
before Step 0 (they resurface as Q4 / the systemd question / the OS below):

1. **Mattermost — reuse or fresh?**
   - *Reuse an existing Mattermost* you administer (admin access + rights to
     create a bot token) → **skip Step 3**, go to **Step 3b** (create the bot).
   - *No Mattermost yet* → **Step 3** stands one up in Docker, then 3b.
   - *Hosted/Cloud Mattermost you can't create bot tokens on* → **stop.** It
     won't work (see Prerequisites) — you need a server you control.

2. **Process supervision — systemd or foreground?**
   - *Linux with systemd* (the default, managed pattern) → **Step 6** installs
     the user-level `agent-chatops.target` from `deploy/systemd/`.
   - *Just evaluating, or no systemd* → run the two `run.sh` scripts in the
     foreground / under `screen` → **Step 6 → Foreground / macOS**.

3. **OS — Linux or macOS?**
   - *Linux* → everything applies as written; the `/proc` codex tie-breaker
     (Step 7c) is available.
   - *macOS* → no systemd and no `/proc`: use **Step 6 → Foreground / macOS**
     (screen or a launchd agent) and expect the less-precise cwd-matched codex
     resolver. Everything else is identical.

> **Unsure? The reference path is: fresh Docker Mattermost → user-level systemd
> on Linux.** That's what the numbered steps assume when a branch isn't called
> out. Every step ends in an executable **✅ Checkpoint** — do not proceed past a
> failing one.

---

## Step 0 — Ask the operator these questions FIRST

Do not run anything until you have answers. Record them in the table below; later steps
reference these names. If the operator says "use your judgement" / "go", pick the
**default** shown and tell them what you chose.

The two services are public repos — you will clone them in Steps 4 and 5:

- agent-harness → `https://github.com/tijszwinkels/agent-harness`
- mm-bridge → `https://github.com/tijszwinkels/mm-bridge`

> **No GitHub access from this host (air-gapped, private repos, blocked egress)?**
> Steps 4 and 5 only need the two repo trees *present* under `<install_dir>` — the
> `git clone` is one way to get them there, not the only way. Populate the install
> dir another way instead (e.g. `rsync -a --exclude .venv --exclude '.git'` from a
> host that has them, or unpack a tarball) and skip the two `git clone` lines. The
> rest of the runbook is identical. Do **not** copy any `.env`, `run.sh`,
> `config.toml`, or state files across hosts — those are per-host and recreated
> from the templates here.

### 0a. Host & backends (blocking — you cannot proceed without these)

| # | Question | Notes |
|---|---|---|
| 1 | **Host & OS user** — which machine, and which non-root user owns the repos and runs the agents? | All host services run as this user. |
| 2 | **Install directory** — where the two repos are cloned. | Default `~/.local/opt/agent-chatops/` (→ `.../mm-bridge` + `.../agent-harness`). Don't assume `~/projects`. |
| 3 | **Backend credentials** — which coding agents should work: `claude`, `codex`, `pi`? For each, is the CLI installed and **logged in as this user**, or do I need to authenticate it? | At least one is required. Have API keys / logins ready. If a CLI login is interactive, stop and ask the operator to complete it. |

### 0b. Mattermost

| # | Question | Default |
|---|---|---|
| 4 | **New Mattermost, or reuse an existing one?** If existing, give me its URL + admin access and skip Step 1. | New container |
| 5 | **Where will Mattermost be reachable (`DOMAIN`)?** Any of these is fine — ask the operator, don't assume: `localhost` (single-host / eval), a Tailnet hostname (tailnet-only access), or a public domain. | `localhost` |
| 6 | **TLS?** nginx variant (HTTPS on :443) or plain `:8065` behind your own proxy / Tailscale. | Plain `:8065` |
| 7 | **`public_url`** — the URL humans actually click (may differ from where the daemon reaches MM, e.g. a Tailscale hostname). | `http://<DOMAIN>:8065` |
| 8 | **Team slug** (`MM_TEAM`) the bot operates in. | Ask — must be created |

### 0c. The bot & session behaviour

| # | Question | Default |
|---|---|---|
| 9  | **Bot username** — the `@name` the agent posts as. | `b3mo` |
| 10 | **Auto-join public channels?** If on, the bot silently joins every public channel it can see (sessions still created only on first engagement). If off, someone must `/invite @<bot>` per channel. | **off** |
| 11 | **Autorespond default** — reply to every message, or only when `@mentioned`? | **mention-only** (off) |
| 12 | **Default backend** for new channels — `claude`, `codex`, and `pi` are all first-class. Sensible default: **whichever agent is running this install**, since it's demonstrably installed + authed on this host. | the installing agent |
| 13 | **Default model** per backend (`claude` / `codex`). **`pi` needs none** — the harness is model-optional for it (agent-harness PR #34, deployed). | `claude=opus, codex=gpt-5.5` |
| 14 | **`default_cwd`** — working dir new sessions start in: the user's *code* root, **distinct from the install dir** (Q2 — tooling, not workspace). Only a default: any channel can override it in-chat with `.cwd <path>` (persisted in its Channel Purpose), and `mm-bridge spawn --cwd` overrides it per spawn. | `~/projects` |
| 15 | **`allowed_attachment_roots`** — directories the bridge may upload files from via `<openFile>`. Include a scratch dir (`~/mm-attachments`) so agents have an unambiguous place to stage generated files — otherwise they reach for `/tmp`, which is outside the roots and silently fails to attach. These roots also bound the per-channel cwd overrides (`.cwd <path>` / `cwd=` in the Channel Purpose), so keep the user's code root in the list. | `["~/projects", "~/mm-attachments"]` |
| 16 | **Show tool-use posts?** Coalesced per-turn tool-use placeholders, or hide them (only real replies + errors). | show |

> **Fill this in before continuing:**
> host=`____` user=`____` install_dir=`____` backends=`____`
> DOMAIN=`____` tls=`____` public_url=`____` MM_TEAM=`____` bot=`____`
> auto_join=`____` autorespond=`____` default_backend=`____` default_cwd=`____`

---

## Step 1 — Prerequisites

**What this stack assumes** (the same list as the README *Requirements* — mm-bridge is
glue, it bundles none of these):

- **A self-hosted Mattermost you administer** — or the willingness to run one (Step 3).
  A hosted/Cloud MM you can't mint bot tokens on won't work.
- **agent-harness on this host** — cloned + runnable (Step 4). The bridge is useless
  without one reachable.
- **At least one agent CLI (`claude` and/or `codex`) installed and logged in as the host
  user**, on this same machine (Step 2).
- **Linux preferred** — the `/proc` codex tie-breaker is Linux-only (macOS falls back to
  the less-precise cwd scan; see the Decision tree).
- **Python 3.11+.**

Now install and verify the toolchain **as the host user**:

```bash
# Docker + compose plugin (for Mattermost)
docker --version && docker compose version

# uv (both Python services use it)
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh

# Node (for the claude CLI), plus screen / jq / git
node --version; screen --version | head -1; jq --version; git --version
```

**✅ Checkpoint:** every command above prints a version. Docker can run containers as this
user (`docker run --rm hello-world`).

> **🔴 Security — public-IP host?** The agent-harness API (Step 4) is **unauthenticated**
> and spawns backend CLIs with `--dangerously-skip-permissions`. Anyone who can reach its
> port (default `8877`) gets remote code execution as this user. Keep it bound to
> **loopback** (`--host 127.0.0.1`, the default in Step 4's template) — the bridge reaches
> it over localhost, so nothing else needs to. **Never bind `0.0.0.0` on a box with a
> public IP.** If this host is internet-facing, also firewall everything except SSH (22) and
> the Mattermost port before proceeding — e.g. `sudo ufw default deny incoming; sudo ufw
> allow 22; sudo ufw allow 8065; sudo ufw enable` (adjust the MM port for your Q6 variant).
> Serve Mattermost over TLS when `DOMAIN` is a public hostname — plain `:8065` sends the
> bot token and passwords in cleartext.

---

## Step 2 — Backend coding-agent CLIs

agent-harness runs `claude`, `codex`, and `pi` as subprocesses inheriting its PATH — all
three are first-class. Install **at least the default backend** (Q12) and authenticate **as
the host user** (not root). If an agent is *running this install*, its own backend is already
installed + authed here — the natural default; just add whichever others you chose in Q3.
Claude Code as a worked example:

```bash
# Claude Code → installs into ~/.npm-global/bin (which the harness puts on PATH)
npm config set prefix ~/.npm-global
export PATH="$HOME/.npm-global/bin:$PATH"
npm install -g @anthropic-ai/claude-code
claude    # run once and complete login / API-key setup

# codex / pi: install their CLIs and authenticate the same way (pi needs no model config)
```

**✅ Checkpoint:** `claude --version` (and any other chosen backend) resolves, and a trivial
prompt works when run manually as this user. If a CLI lives outside `~/.npm-global/bin`,
note its directory — you'll adjust the harness `run.sh` PATH line in Step 4.

---

## Step 3 — Mattermost (container install)

Skip if reusing an existing instance (Q4) — go to Step 3b to create the bot.

Official quick-start (`github.com/mattermost/docker`):

```bash
git clone https://github.com/mattermost/docker
cd docker
cp env.example .env
# Edit .env:
#   DOMAIN=<DOMAIN>                              (from Q5)
#   MM_SERVICESETTINGS_SITEURL=<site_url>        (see below — MUST match how clients reach MM)

mkdir -p ./volumes/app/mattermost/{config,data,logs,plugins,client/plugins,bleve-indexes}
sudo chown -R 2000:2000 ./volumes/app/mattermost
```

Bring it up — pick the variant from Q6:

```bash
# Plain HTTP on :8065  (evaluation / behind your own proxy)
docker compose -f docker-compose.yml -f docker-compose.without-nginx.yml up -d

# OR HTTPS via bundled nginx
# docker compose -f docker-compose.yml -f docker-compose.nginx.yml up -d
```

> **`MM_SERVICESETTINGS_SITEURL` per variant (Q6).** SiteURL must equal the origin clients
> actually use, or logins and the bot WebSocket break:
> - **Plain `:8065`** → `http://<DOMAIN>:8065` (note the port and **http**, not https).
> - **Bundled nginx / your own TLS proxy** → `https://<DOMAIN>` (no port).
>
> For `DOMAIN=localhost` the plain SiteURL is `http://localhost:8065`.

> **Host already runs an `nginx-proxy` (auto-vhost + acme-companion)?** Don't use the
> bundled nginx — you'll get a cert/routing conflict. Bring Mattermost up with the
> **without-nginx** compose, then attach the `mattermost` service to the proxy's network and
> hand it the discovery/cert env vars via an override file:
>
> ```yaml
> # docker-compose.override.yml  (alongside the mattermost compose files)
> services:
>   mattermost:
>     networks: [proxy]
>     environment:
>       VIRTUAL_HOST: <DOMAIN>
>       VIRTUAL_PORT: "8065"
>       LETSENCRYPT_HOST: <DOMAIN>
>       LETSENCRYPT_EMAIL: <you@example.com>   # ← placeholder — set a real address
>       MM_SERVICESETTINGS_SITEURL: https://<DOMAIN>
> networks:
>   proxy:
>     external: true
>     name: <proxy-network-name>               # ← placeholder — your proxy's docker network
> ```
>
> Then bring it up including the override:
> `docker compose -f docker-compose.yml -f docker-compose.without-nginx.yml -f docker-compose.override.yml up -d`.
> (Env-var names follow the standard `nginx-proxy` / `acme-companion` convention; the
> network name and email are host-specific — confirm them with the operator.)

Now create the **system-admin** user and the **team**. This runbook's audience is an AI
agent with no browser, so the **headless REST-API path is primary** (the browser is only an
alternative — see the note at the end of this step).

**On a brand-new server the *first* user to register is automatically granted the
`system_admin` role** — so create it via the API. Generate a strong password, store it
`chmod 600`, and never echo it into the channel or logs:

```bash
# 1. Server is up?
curl -sSf http://localhost:8065/api/v4/system/ping    # → {"status":"OK"}

# 2. Generate + persist the admin credentials FIRST (so a crash can't lose the password).
mkdir -p ~/.config/mm-bridge
ADMIN_PW=$(openssl rand -base64 24)
umask 077
cat > ~/.config/mm-bridge/admin.env <<ENV
MM_ADMIN_USER=admin
MM_ADMIN_EMAIL=<admin_email>          # Q — a real address you control
MM_ADMIN_PASSWORD=$ADMIN_PW
ENV
chmod 600 ~/.config/mm-bridge/admin.env
# ^ The MM admin password lives here alongside config.toml/env, chmod 600, out of the repo.
#   Do not print $ADMIN_PW to the channel. Recover it later with:
#   set -a; source ~/.config/mm-bridge/admin.env; set +a

# 3. First user → becomes system admin (no auth needed for the very first user).
curl -sSf -X POST http://localhost:8065/api/v4/users \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"<admin_email>\",\"username\":\"admin\",\"password\":\"$ADMIN_PW\"}" >/dev/null

# 4. Log in as admin → capture the session token from the `Token` response header.
ADMIN_TOKEN=$(curl -sS -D - -o /dev/null -X POST http://localhost:8065/api/v4/users/login \
  -H 'Content-Type: application/json' \
  -d "{\"login_id\":\"admin\",\"password\":\"$ADMIN_PW\"}" \
  | awk 'tolower($1)=="token:"{print $2}' | tr -d '\r')
[ -n "$ADMIN_TOKEN" ] || { echo "admin login failed"; exit 1; }

# 5. Create the team. `name` is the URL slug = MM_TEAM (Q8); type O = open.
curl -sSf -X POST http://localhost:8065/api/v4/teams \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"<MM_TEAM>","display_name":"<MM_TEAM>","type":"O"}' >/dev/null
```

> **`mmctl` alternative.** If you have the Mattermost CLI, the same two objects are
> `mmctl user create --system-admin --email <admin_email> --username admin --password "$ADMIN_PW"`
> and `mmctl team create --name <MM_TEAM> --display-name <MM_TEAM>` (after
> `mmctl auth login http://localhost:8065 --name local --username admin --password "$ADMIN_PW"`).

> **Browser alternative.** If a human with a browser is available, open
> `http://<DOMAIN>:8065/` (plain variant) or `https://<DOMAIN>/` (nginx/TLS), create the
> system-admin user on the first-run screen, then create the team — its slug is `MM_TEAM`.
> Still record the admin password in `~/.config/mm-bridge/admin.env` (chmod 600) as above.

**✅ Checkpoint:** `curl -sSf http://localhost:8065/api/v4/system/ping` returns
`{"status":"OK"}`, `~/.config/mm-bridge/admin.env` exists (chmod 600), and the admin login
in step 4 yielded a non-empty `$ADMIN_TOKEN`.

### Step 3b — Bot account + personal access token

Headless path (primary), continuing as the admin from Step 3. Re-establish an admin token
first (a fresh shell won't have `$ADMIN_TOKEN` from Step 3):

```bash
set -a; source ~/.config/mm-bridge/admin.env; set +a
ADMIN_TOKEN=$(curl -sS -D - -o /dev/null -X POST http://localhost:8065/api/v4/users/login \
  -H 'Content-Type: application/json' \
  -d "{\"login_id\":\"$MM_ADMIN_USER\",\"password\":\"$MM_ADMIN_PASSWORD\"}" \
  | awk 'tolower($1)=="token:"{print $2}' | tr -d '\r')

# 1. Enable bot-account creation + personal access tokens (System Console flags, via API).
curl -sSf -X PUT http://localhost:8065/api/v4/config/patch \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"ServiceSettings":{"EnableBotAccountCreation":true,"EnableUserAccessTokens":true}}' >/dev/null

# 2. Create the bot (Q9) and capture its user id.
BOT_USER_ID=$(curl -sSf -X POST http://localhost:8065/api/v4/bots \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"username":"<bot>","display_name":"<bot>"}' | jq -r .user_id)

# 3. Add the bot to the team (it must be a member to see/post in channels).
TEAM_ID=$(curl -sSf http://localhost:8065/api/v4/teams/name/<MM_TEAM> \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r .id)
curl -sSf -X POST http://localhost:8065/api/v4/teams/$TEAM_ID/members \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"team_id\":\"$TEAM_ID\",\"user_id\":\"$BOT_USER_ID\"}" >/dev/null

# 4. Mint the bot's personal access token → this value is MM_BOT_TOKEN.
curl -sSf -X POST http://localhost:8065/api/v4/users/$BOT_USER_ID/tokens \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"description":"mm-bridge"}' | jq -r .token
```

The `token` printed by step 4 is **`MM_BOT_TOKEN`** — put it in `~/.config/mm-bridge/env`
(Step 5b), never in the repo or the channel. (`mmctl` equivalent:
`mmctl bot create <bot> --display-name <bot>` then
`mmctl token generate <bot> mm-bridge`, after `mmctl auth login`.)

> **Browser alternative.** In **System Console**: **Integrations → Bot Accounts** → *Enable
> Bot Account Creation* = true; **Integrations → Integration Management** → *Enable Personal
> Access Tokens* = true; then **Integrations → Bot Accounts → Add Bot Account**
> (username `<bot>`, Q9) and create a token — **copy it immediately, it is shown once.**
> Add the bot to the team.

**✅ Checkpoint:** the token works and resolves to the bot:

```bash
curl -s -H "Authorization: Bearer <MM_BOT_TOKEN>" \
  http://localhost:8065/api/v4/users/me | jq .username    # → "<bot>"
```

---

## Step 4 — agent-harness

Clone into the install directory (Q2) and sync. Example uses the default
`~/.local/opt/agent-chatops/`:

```bash
mkdir -p ~/.local/opt/agent-chatops   # <install_dir> from Q2 (create it — don't assume it exists)
cd ~/.local/opt/agent-chatops
git clone https://github.com/tijszwinkels/agent-harness
cd agent-harness
uv sync
cp run.sh.example run.sh && chmod +x run.sh   # run.sh is gitignored (per-host) — create it from the template
```

`run.sh` is gitignored (per-host), so you create it from `run.sh.example`. It must
(a) put the backend CLIs **and** `uv` on PATH and (b) pass `--execute-runs`. The template:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"   # ← adjust if backends live elsewhere (Step 2)
exec uv run agent-harness serve \
  --host 127.0.0.1 --port 8877 \              # ← loopback: the API is UNAUTHENTICATED (see Step 1). Never 0.0.0.0 on a public box.
  --database "$HOME/.local/state/agent-harness/harness.db.8877" \   # ← state OUT of the repo (XDG state dir)
  --execute-runs                              # ← REQUIRED: without it, runs are recorded but no CLI launches
  # --cors-origin https://your-dashboard      # only if a browser app calls the harness cross-origin
```

> **Why loopback?** The bridge reaches the harness at `http://localhost:8877`, so nothing
> needs a wider bind. The harness has no auth and launches agents with
> `--dangerously-skip-permissions`; `0.0.0.0` on a public-IP host is unauthenticated remote
> code execution (see the Step 1 security callout). Widen the bind only behind a firewall +
> authenticating proxy — the harness logs a startup WARNING whenever `--host` isn't loopback.

> The stock `run.sh.example` writes the DB into the clone (`.agent-harness.db.8877`);
> point `--database` at `~/.local/state/agent-harness/` so state survives a re-clone and
> the repo stays clean.

Create the state directory (the DB lives here, outside the repo), then start it once in
the foreground to verify, and Ctrl-C:

```bash
mkdir -p ~/.local/state/agent-harness
./run.sh &
sleep 3
curl -s localhost:8877/v1/health              # → ok
ls ~/.local/state/agent-harness/harness.db.*  # → DB created here, not in the clone
kill %1
```

**✅ Checkpoint:** `/v1/health` responds, `--execute-runs` is present in `run.sh`, and the
harness DB file exists under `~/.local/state/agent-harness/` (not in the clone).

> Notes: `--execute-runs` is what actually spawns `claude`/`codex`; omit it and you get a
> dry recorder. By default the harness also *observes* `~/.claude`, `~/.codex`, `~/.pi`
> transcript roots so terminal sessions appear too (`--no-observer` disables).
>
> PATH: the `export PATH=` line assumes `claude` came from `npm -g` (→ `~/.npm-global/bin`).
> If your backends are **system-installed** (e.g. `/usr/bin/claude`) they're already on PATH
> and you can drop that dir — but keep `~/.local/bin` on PATH so `uv run` resolves under
> systemd's non-interactive shell (e.g. `export PATH="$HOME/.local/bin:$PATH"`).

---

## Step 5 — mm-bridge

```bash
cd ~/.local/opt/agent-chatops    # <install_dir> from Q2 (created in Step 4)
git clone https://github.com/tijszwinkels/mm-bridge
cd mm-bridge
uv sync                          # creates .venv + installs deps
cp run.sh.example run.sh && chmod +x run.sh   # run.sh is gitignored (per-host) — create it from the template
```

> Use `uv sync`, **not** `uv pip install -e .` — the latter needs an already-active venv
> and fails here with *"No virtual environment found"*.

`run.sh` is gitignored (per-host), so you create it from `run.sh.example`. The template
launches via `uv run` — a bare `mm-bridge serve` only works if the console script is on
PATH, which it isn't after a plain `uv sync`:

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"   # so `uv` resolves under systemd's non-interactive shell
# Secrets: prefer the XDG config dir (chmod 600, outside the repo);
# fall back to a repo-local .env for compatibility.
if [ -f "$HOME/.config/mm-bridge/env" ]; then set -a; source "$HOME/.config/mm-bridge/env"; set +a
elif [ -f .env ]; then set -a; source .env; set +a; fi
exec uv run mm-bridge serve
```

### 5a. Config file — `~/.config/mm-bridge/config.toml`

Fill from the Step 0 answers:

> **TOML gotcha — key order matters.** In TOML, every key after a `[section]` header
> belongs to that section. The bridge reads the session defaults below from the **top
> level** of the file, so they must appear **before** any `[mattermost]` / `[agent_harness]`
> header. Put them under a section and they get nested there, are silently ignored, and you
> fall back to built-in defaults (e.g. `auto_join`/`autorespond` stay off no matter what you
> wrote). For the on/off flags, the `.env` overrides in 5b are the most reliable route.

```toml
# ── Top-level keys — MUST come before the [sections] below ──
default_backend           = "<default_backend>"   # Q12
default_cwd               = "<default_cwd>"         # Q14
default_autorespond       = false                  # Q11 (true = reply to every message)
auto_join_public_channels = false                  # Q10 (true = bot joins all public channels)
show_tool_use             = true                   # Q16
allowed_attachment_roots  = ["~/projects", "~/mm-attachments"]   # Q15 (scratch dir for agent-generated attachments)
default_models            = { claude = "opus", codex = "gpt-5.5" }   # Q13 — pi needs none (harness is model-optional)
state_file                = "~/.config/mm-bridge/state.json"
sidecar_dir               = "~/.mm-bridge/sessions"

[mattermost]
url        = "localhost"
port       = 8065
scheme     = "http"
team       = "<MM_TEAM>"                       # Q8
public_url = "<public_url>"                    # Q7 — the URL humans click

[agent_harness]
url = "http://localhost:8877"
```

> **Create `default_cwd` if it's missing.** New sessions start there, and it may not exist
> on a fresh box (`~/projects` is *not* the install dir). `mkdir -p "$(eval echo <default_cwd>)"`
> so the first session doesn't land in a non-existent directory.
>
> **Create the attachment scratch dir (Q15).** `mkdir -p ~/mm-attachments` so agents have a
> whitelisted place to stage generated files for `<openFile>`. Without it the agent falls back
> to `/tmp`, which is outside `allowed_attachment_roots` and can't be attached.

### 5b. Secrets — `~/.config/mm-bridge/env` (chmod 600)

The bot token is read from the **environment** — there is no TOML key for it — so it lives
in a secrets file the daemon sources at launch. Keep it in `~/.config/mm-bridge/env`,
outside the repo and next to `config.toml`. (`run.sh` sources `~/.config/mm-bridge/env`
when present, else falls back to a repo-local `.env`. That repo-`.env` pattern still works,
but it's a **compatibility fallback** — not the recommended home for secrets.)

```bash
mkdir -p ~/.config/mm-bridge
cat > ~/.config/mm-bridge/env <<'ENV'
MM_BOT_TOKEN=<MM_BOT_TOKEN>          # from Step 3b — REQUIRED
MM_URL=http://localhost:8065
MM_TEAM=<MM_TEAM>
MM_PUBLIC_URL=<public_url>
AH_URL=http://localhost:8877

# Flag overrides — env wins over BOTH the TOML and the built-in defaults, so this
# is the most reliable way to turn a flag on. Uncomment only what you want ON.
# MM_AUTO_JOIN=true                    # Q10 — bot silently joins all public channels
# MM_BRIDGE_DEFAULT_AUTORESPOND=true   # Q11 — reply to every message, not just @mentions
ENV
chmod 600 ~/.config/mm-bridge/env
```

**✅ Checkpoint:** run `./run.sh` in the foreground. It logs a successful Mattermost
WebSocket connection and an agent-harness SSE subscription, with no auth errors. Ctrl-C.

Then run the built-in diagnostics — config + Mattermost auth + sidecar dir should be ✓
(the agent-harness line is ✓ only if Step 4's harness is still up; it goes green for good
once Step 6 supervises it):

```bash
set -a; source ~/.config/mm-bridge/env; set +a
uv run mm-bridge doctor    # → ✓ config, ✓ mattermost (prints the resolved @<bot>), ✓ sidecar-dir
```

> **PATH:** `uv run …` needs the `uv` binary on PATH, which lives in `~/.local/bin` — not on
> PATH in a bare non-login shell. If you get `uv: command not found`, run
> `export PATH="$HOME/.local/bin:$PATH"` first (and run this from inside the mm-bridge repo
> dir, since `uv run` resolves the project there). After **Step 7a** installs the console
> script, the bare `mm-bridge doctor` works from any cwd — no `uv run`.

---

## Step 6 — Supervise both as a systemd **user** target (Linux)

> **No systemd (macOS, or just evaluating)?** Skip to *Foreground / macOS* below.

The repo ships ready-made unit files in **`deploy/systemd/`** — install those rather than
hand-writing units (they encode the dependency shape deliberately):

- `agent-chatops.target` — the whole stack; `Wants=` both services. One handle to
  start/stop/restart the pair.
- `agent-harness.service` — `PartOf=` the target.
- `mm-bridge.service` — `PartOf=` the target, `After=` the harness for **soft** start
  ordering only. Deliberately **not** `BindsTo=`/`Requires=`: the bridge reconnect-resumes
  the harness SSE on its own, and hard coupling would mask a regression in that reconnect
  path behind systemd restarts.

Copy them in and fill in the two `ExecStart=` paths (they default to
`%h/.local/opt/agent-chatops/...` — adjust if your `<install_dir>` from Q2 differs; `run.sh`
handles PATH + secrets, so nothing sensitive lives in the units):

```bash
mkdir -p ~/.config/systemd/user
cp ~/.local/opt/agent-chatops/mm-bridge/deploy/systemd/agent-chatops.target \
   ~/.local/opt/agent-chatops/mm-bridge/deploy/systemd/agent-harness.service \
   ~/.local/opt/agent-chatops/mm-bridge/deploy/systemd/mm-bridge.service \
   ~/.config/systemd/user/

# Verify / fix the ExecStart run.sh paths for THIS host's <install_dir>:
${EDITOR:-nano} ~/.config/systemd/user/agent-harness.service
${EDITOR:-nano} ~/.config/systemd/user/mm-bridge.service
```

Enable the target and make it survive logout/reboot **without an interactive login**:

```bash
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now agent-chatops.target
systemctl --user status agent-chatops.target agent-harness.service mm-bridge.service
```

The target's `Wants=` starts both services — you don't enable them individually. Logs go to
journald:

```bash
journalctl --user -u agent-harness -f
journalctl --user -u mm-bridge -f
```

**✅ Checkpoint:** all three units are `active (running)`, and the bridge's own diagnostics
are all-green:

```bash
set -a; source ~/.config/mm-bridge/env; set +a
cd ~/.local/opt/agent-chatops/mm-bridge && uv run mm-bridge doctor    # → every line ✓, exit 0
```

`mm-bridge doctor` checks config keys, Mattermost auth (printing the resolved `@<bot>`
username), agent-harness reachability, and sidecar-dir writability in one shot — the
fastest confirmation the stack is wired up. A ✗ on any line stops you here.

### Foreground / macOS (no systemd)

systemd user units are Linux-only. Elsewhere, run the two `run.sh` scripts directly — each
`exec`s its daemon in the foreground:

```bash
# Terminal 1  (or detached: screen -dmS harness ~/.local/opt/agent-chatops/agent-harness/run.sh)
~/.local/opt/agent-chatops/agent-harness/run.sh
# Terminal 2  (or detached: screen -dmS mmbridge ~/.local/opt/agent-chatops/mm-bridge/run.sh)
~/.local/opt/agent-chatops/mm-bridge/run.sh
```

For unattended restarts on **macOS**, wrap each `run.sh` in a launchd agent
(`~/Library/LaunchAgents/*.plist`, `KeepAlive=true`) — the launchd analogue of the systemd
units above. `screen`/`tmux` is fine for evaluation.

**✅ Checkpoint (same as above):** with both `run.sh` processes up, `mm-bridge doctor` is
all-green:

```bash
set -a; source ~/.config/mm-bridge/env; set +a
cd ~/.local/opt/agent-chatops/mm-bridge && uv run mm-bridge doctor    # → every line ✓, exit 0
```

---

## Step 7 — Make the `mm-bridge` CLI + its instructions available to sessions

A coding-agent session can drive the bridge from *inside* a channel — read scrollback
(`mm-bridge read`), pull in a human (`invite`), spawn a sibling session (`spawn`), post
cross-channel, etc. **This is where a fresh host silently differs from the reference host,**
and it's easy to miss because nothing errors — the model just never uses the CLI. It needs
three things that don't exist by default:

### 7a. The `mm-bridge` CLI on PATH

`uv sync` only puts the console script in the repo's `.venv`, so a session's shell can't
find it. Install it onto the user's PATH (matches the reference host's `~/.local/bin/mm-bridge`):

```bash
uv tool install ~/.local/opt/agent-chatops/mm-bridge   # → ~/.local/bin/mm-bridge (ensure ~/.local/bin is on PATH)
mm-bridge --help                                        # sanity
```

Installing from the clone path (rather than `cd`-ing in and using `.`) keeps this a single
recommended command that works from any cwd. The harness `run.sh` already prepends
`~/.local/bin`, so agent sessions it spawns inherit the CLI on PATH. (Re-run
`uv tool install --reinstall ~/.local/opt/agent-chatops/mm-bridge` after pulling updates.)

### 7b. Teach the agents the CLI exists — inject the cheat-sheet

**The CLI is useless if the model doesn't know it's there.** On the reference host, every
Claude Code session gets the bridge's usage guide because the user's **global** instructions
import it. Reproduce that — add the import to `~/.claude/CLAUDE.md` (create the file if it
doesn't exist):

```
@~/.local/opt/agent-chatops/mm-bridge/CLAUDE-include.md
```

(Use your actual `<install_dir>` path.) `CLAUDE-include.md` ships in the repo and documents
`read` / `channels` / `post` / `invite` / `spawn`, the in-channel dot-commands, and how to
`source .env` for `MM_BOT_TOKEN`. **Without this import a session has no idea
`mm-bridge read --channel <id>` even exists** — the exact failure seen on the remote.

> **Codex backend?** Claude reads `~/.claude/CLAUDE.md`; Codex reads `AGENTS.md`. Put the
> same guidance where your Codex sessions load global instructions (e.g.
> `~/.codex/AGENTS.md`). Confirm the exact path/import mechanism for your Codex version —
> it differs from Claude's `@import`, so you may need to inline the content rather than
> `@`-import it.

### 7c. SessionStart hook (Claude Code) — session self-identification

So a session can resolve *which* channel it's bound to (needed by `mm-bridge channel` /
`invite` / `spawn`), install `~/.claude/hooks/export-session-id.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0
sid=$(jq -r '.session_id // empty')
[ -n "$sid" ] || exit 0
printf 'export CLAUDE_SESSION_ID=%q\n' "$sid" >> "$CLAUDE_ENV_FILE"
```

`chmod +x` it and register it as a `SessionStart` hook in `~/.claude/settings.json`.
(Codex needs no hook — the bridge discovers its session via `/proc` and the rollout file.)

**✅ Checkpoint:** `mm-bridge --help` runs from a plain shell; `~/.claude/CLAUDE.md`
contains the `@…/CLAUDE-include.md` import; and in a Claude Code session bound to a channel,
`mm-bridge channel` prints a channel id (not "not in MM channel").

---

## Step 8 — End-to-end smoke test

1. `set -a; source ~/.config/mm-bridge/env; set +a && mm-bridge doctor` → every line ✓,
   exit 0 (config, Mattermost auth + resolved `@<bot>`, agent-harness reachable,
   sidecar-dir writable). Sourcing `.env` gives your shell the same `MM_BOT_TOKEN` the
   daemon uses; this subsumes the old `curl localhost:8877/v1/health` check.
2. In Mattermost, create a channel and `/invite @<bot>` (skip the invite if auto-join is on).
3. Before posting a conversational message, use `.backend <name>`, `.model <name>` and/or
   `.cwd <path>`; confirm no agent session starts yet.
4. Post `@<bot> hello`. Within a few seconds you get a reply from a session using that configuration.
5. Type `.status` → shows session id, backend, model, cwd, autorespond flag, harness status
   (the cwd is whatever `.cwd` set, else `default_cwd`).
6. Type `.help` → lists dot-commands (`.stop`, `.model`, `.cwd`, `.sessions`, `.autorespond`, …).

**✅ Done** when a message to the bot produces a model reply and `.status` reports the
harness as reachable.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| **Anything wrong / unsure where to start** | Run `mm-bridge doctor` — the first ✗ line names the broken layer (config, Mattermost auth, harness, or sidecar dir). |
| Bot never replies | mm-bridge not connected — `journalctl --user -u mm-bridge -f` (systemd) or the `run.sh` terminal (foreground) for logs; check `MM_BOT_TOKEN`, `MM_URL`, team slug. `mm-bridge doctor` isolates it. |
| Reply says harness unreachable | agent-harness down or wrong `AH_URL`; `mm-bridge doctor` (agent-harness line) or `curl localhost:8877/v1/health`. |
| Turn recorded but no output | `--execute-runs` missing from harness `run.sh`. |
| `FileNotFoundError: claude/codex` | Backend CLI not on the harness PATH — fix the `export PATH=` line in `run.sh` (systemd's non-interactive shell skips `~/.bashrc`). |
| Services don't start after reboot | `loginctl enable-linger "$USER"` not set. |
| `<openFile>` uploads nothing | Path is outside `allowed_attachment_roots` (the bridge posts a warning listing the roots that *are* allowed). Stage the file in `~/mm-attachments` (Q15) or the session cwd, or add its dir to `allowed_attachment_roots`. |
| `mm-bridge` says "not in MM channel" | SessionStart hook missing (claude) or invoked in the startup race before the sidecar exists. |
| Bot works but never uses the CLI (won't read scrollback, `invite`, `spawn`) | The session doesn't know the CLI exists / can't find it. Import `CLAUDE-include.md` into `~/.claude/CLAUDE.md` (Step 7b) and put `mm-bridge` on PATH via `uv tool install ~/.local/opt/agent-chatops/mm-bridge` (Step 7a). |
| `mm-bridge: command not found` inside a session | CLI not on the session's PATH — `uv tool install ~/.local/opt/agent-chatops/mm-bridge` (Step 7a); confirm `~/.local/bin` is on PATH. |
| `Error: MM_BOT_TOKEN environment variable is required` (CLI) | Session shell didn't load the token — `set -a; source ~/.config/mm-bridge/env; set +a` (or the repo-local `.env` fallback; documented in `CLAUDE-include.md`). |

---

## Sources

- Mattermost container install — https://github.com/mattermost/docker ·
  https://docs.mattermost.com/deployment-guide/server/deploy-containers.html
- Bot accounts / personal access tokens —
  https://developers.mattermost.com/integrate/reference/bot-accounts/ ·
  https://developers.mattermost.com/integrate/reference/personal-access-token/
- Service details — this repo's `README.md`, `run.sh`, and the agent-harness `README.md` /
  `run.sh`. (Mattermost docs retrieved 2026-07-09.)
