# Agent Fleet

A read-only local dashboard that shows every running Claude agent — from both
the Claude Code CLI and the native Claude desktop app — on one page in your
browser.

## What it does

Agent Fleet polls the files Claude already writes to disk and turns them into
a live-updating grid of cards: one per session, showing status, model,
working directory / git branch, last tool call, sub-agent dispatches, token
counts, and an estimated dollar cost. It refreshes itself; you don't need to
reload the page.

It shows any session active in the last 24 hours, ranked with the sessions
most likely to need you at the top.

## Requirements

- **macOS.** See [Why macOS only](#why-macos-only).
- **Python 3.9 or newer.** The system `/usr/bin/python3` is enough — the
  project has no third-party dependencies and there is nothing to `pip
  install`.
- At least one Claude agent that has run on this machine, so there is
  something to read.

## Quick start

Run it in the foreground first. Nothing is installed and nothing persists, so
this is the cheapest way to see whether it works on your machine:

```bash
git clone https://github.com/joekogit/agent-fleet.git
cd agent-fleet
python3 -m fleet
```

Open <http://127.0.0.1:8787>. Press Ctrl-C to stop.

If the page loads but says "No agents active in the last 24 hours", the server
is fine and simply found no data — see [Configuration](#configuration).

Once you're happy with it, install it as a background service so it starts at
login:

```bash
./install.sh
```

## Data sources

Agent Fleet reads two places on disk, and only reads them — see
[Read-only](#read-only) below.

1. **Claude Code CLI** — `~/.claude/sessions/<PID>.json` for live process
   info, plus the matching transcript under `~/.claude/projects/**/<sessionId>.jsonl`
   for tokens, tool calls, git branch, and sub-agent activity.
2. **Claude desktop app** — the session and transcript files under
   `~/Library/Application Support/Claude`.

If one adapter fails to read a file (corrupt JSON, a session mid-write), that
one session is skipped or shown with an error badge; it never blanks out the
rest of the fleet.

## Configuration

Everything is optional; the defaults are the standard Claude locations.

| Variable | Default | What it does |
| --- | --- | --- |
| `AGENT_FLEET_HOME` | `~` | The home directory the CLI adapter looks under, i.e. where it expects `.claude/`. Point this at a second Claude install or a copy of another machine's data. |
| `AGENT_FLEET_APP_SUPPORT` | `~/Library/Application Support/Claude` | The desktop app's support directory. |
| `PORT` | `8787` | Read by `install.sh` only. |

Both paths are `~`-expanded, and an exported-but-empty value is treated as
unset. The foreground server also takes `--port` and `--host`:

```bash
python3 -m fleet --port 9000
AGENT_FLEET_HOME=~/other-machine python3 -m fleet
PORT=9000 ./install.sh
```

`install.sh` copies whichever of the two path variables are set at install
time into the launchd job, since a launchd service inherits nothing from your
shell. If you change them later, re-run `./install.sh` to update the service.

`--host` only accepts a loopback address; anything else is refused rather than
warned about. See [Read-only](#read-only).

## Install / uninstall

```bash
./install.sh
```

This installs a `launchd` user agent (`com.agentfleet.dashboard`) that
starts the dashboard at login and restarts it automatically if it ever
crashes. It's idempotent — safe to re-run any time you pull new code, to pick
up changes. It checks your Python version, waits for the port to actually
answer before reporting success, and warns you if it can't find any Claude
data to read.

By default it serves on port 8787. Override with `PORT=<n> ./install.sh`.

```bash
./uninstall.sh
```

Stops and removes the `launchd` agent. It leaves the repo untouched — run it
any time you want to stop the dashboard for good.

Logs live at `~/Library/Logs/agent-fleet.log`.

## Read-only

Agent Fleet never writes, moves, or deletes anything under `~/.claude/` or
`~/Library/Application Support/Claude/`. The only things it writes are its own
log file (`~/Library/Logs/agent-fleet.log`), its `launchd` plist
(`~/Library/LaunchAgents/`), and files inside this repo.

The server also only binds `127.0.0.1` (loopback). It is not reachable from
your network — the page shows working directories and prompt fragments, so
that matters. Non-loopback `Host` headers are rejected too, so a DNS-rebinding
attempt from a page in your browser cannot reach it either.

## Using it as an app window

The dashboard is a plain web page, but Chrome can give it its own window and
Dock icon so it feels like a native app:

1. Open `http://127.0.0.1:8787` in Chrome.
2. Click the ⋮ menu → **Cast, Save & Share** → **Install page as app**.
3. It gets its own Dock icon and window. ⌘-Tab to it like any other app.

To remove it later, go to `chrome://apps`, right-click the Agent Fleet icon,
and remove it (`./uninstall.sh` only removes the `launchd` agent, not the
Chrome app shortcut).

## Editing prices

All dollar figures on the dashboard are **estimates**. The transcripts record
token counts, never dollar amounts — cost data does not exist anywhere Agent
Fleet reads. Every price shown is computed from a rate table you edit
yourself: `fleet/prices.py`.

Open that file and check the `PRICES` dict against current published
model pricing before trusting any number the dashboard shows — the shipped
values are starting points and go stale whenever pricing changes. A model
missing from the table shows no cost rather than a plausible-looking wrong
one.

## Development

```bash
python3 -m unittest discover tests
```

No dependencies, no build step, no test runner to install.

Two of those tests (`tests/test_smoke.py`) run against whatever real Claude
data is on your machine rather than fixtures; every other test uses the
fixtures in `tests/fixtures/`. The cache-performance one skips itself if you
don't have enough transcript history for its measurement to mean anything, so
a fresh machine still gets a green suite.

## Why macOS only

Two things tie it to macOS: the desktop app adapter reads
`~/Library/Application Support/Claude`, and `install.sh` builds a `launchd`
user agent.

Neither is fundamental. The CLI half reads `~/.claude`, which is the same
everywhere, and the process-liveness checks are plain POSIX — so the core
would run on Linux with a `systemd --user` unit in place of the launchd one
and a corrected desktop path. That work simply hasn't been done or tested,
so the project claims macOS rather than pretending otherwise.

## Known limitations

**Sub-agent visibility is dispatch-and-return only.** Agent Fleet can see
that a sub-agent was dispatched and, once it returns, what it reported back.
It has no view into what a sub-agent is doing while it's running — no
running token count, no in-progress tool calls, nothing. A sub-agent card
just says "running" until it isn't.

**Desktop app sessions can never show `attention`.** The `attention` status —
"this session is probably sitting on a permission prompt, waiting on you" —
is inferred from liveness plus silence: for a CLI session, Agent Fleet checks
that the process is still alive and sees that its transcript has stopped
growing while it's marked busy, and that combination is what triggers
`attention`. It never expires — a session blocked for six hours is still
`attention`. The desktop app exposes no heartbeat and no process to check,
so Agent Fleet has nothing to infer from. A desktop session that's actually
stuck on a permission dialog will show as `busy` or `idle`, indistinguishable
from one that's simply thinking or between turns. If you rely on the desktop
app and need to know when it's waiting on you, you'll need to check it
directly — Agent Fleet cannot tell you.

**A session waiting on its own sub-agent reports `busy`, not `attention`.**
Sub-agent turns are never written to the parent's transcript, so a
dispatching parent looks silent for the whole dispatch — the same silence
that would otherwise mean "waiting on you." Agent Fleet knows the dispatch
is outstanding and reports `busy` instead, with a reason like "working — 1
sub-agent running (5m since its own last write)."

**These file formats are undocumented.** Agent Fleet reads Claude's on-disk
session and transcript files, which are internal and can change without
notice. When that happens the affected sessions degrade to an error badge
rather than taking the page down, but a Claude update can still stop parts of
the dashboard from working until the adapters are updated.

## License

MIT — see [LICENSE](LICENSE).
