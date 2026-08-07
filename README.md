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

## Read-only

Agent Fleet never writes, moves, or deletes anything under `~/.claude/` or
`~/Library/Application Support/Claude/`. The only things it writes are its own
log file (`~/Library/Logs/agent-fleet.log`), its `launchd` plist
(`~/Library/LaunchAgents/`), and files inside this repo.

The server also only binds `127.0.0.1` (loopback). It is not reachable from
your network — the page shows working directories and prompt fragments, so
that matters.

## Install / uninstall

```bash
./install.sh
```

This installs a `launchd` user agent (`com.joekocovsky.agentfleet`) that
starts the dashboard at login and restarts it automatically if it ever
crashes. It's idempotent — safe to re-run any time you pull new code, to pick
up changes.

By default it serves on port 8787. Override with `PORT=<n> ./install.sh`.

```bash
./uninstall.sh
```

Stops and removes the `launchd` agent. It leaves the repo untouched — run it
any time you want to stop the dashboard for good.

Logs live at `~/Library/Logs/agent-fleet.log`.

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

## Known limitations

**Sub-agent visibility is dispatch-and-return only.** Agent Fleet can see
that a sub-agent was dispatched and, once it returns, what it reported back.
It has no view into what a sub-agent is doing while it's running — no
running token count, no in-progress tool calls, nothing. A sub-agent card
just says "running" until it isn't.

**Desktop app sessions can never show `attention`.** The `attention` status —
"this session is probably sitting on a permission prompt, waiting on you" —
is inferred from a heartbeat: the CLI writes activity timestamps as it works,
and a long silence while marked busy is what triggers `attention`. The
desktop app exposes no such heartbeat, so Agent Fleet has nothing to infer
from. A desktop session that's actually stuck on a permission dialog will
show as `busy` or `idle`, indistinguishable from one that's simply thinking
or between turns. If you rely on the desktop app and need to know when it's
waiting on you, you'll need to check it directly — Agent Fleet cannot tell
you.
