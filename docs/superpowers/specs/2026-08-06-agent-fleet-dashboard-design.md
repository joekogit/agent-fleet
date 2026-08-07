# Agent Fleet Dashboard — Design

**Date:** 2026-08-06
**Status:** Approved for planning
**Author:** Joe Kocovsky + Claude

## Problem

Multiple Claude agents run at once across the Claude Code CLI and the native Claude
desktop app. There is no single place to see them. Answering "what is running right
now, and does anything need me?" currently means checking terminals one at a time.

## Goal

An ambient, read-only monitor: one browser page showing every agent session, its
status, what it is doing right now, its sub-agent dispatches, and its token burn.

Explicitly **not** in scope: killing sessions, sending prompts into running agents,
spawning agents. The dashboard never writes to agent state.

## Verified data sources

Both sources were confirmed to exist on this machine on 2026-08-06.

### Source A — Claude Code CLI

- `~/.claude/sessions/<PID>.json` — one file per running process. Fields:
  `pid`, `sessionId`, `cwd`, `startedAt`, `procStart`, `version`, `kind`,
  `entrypoint`, `name` (derived, e.g. `retro-game-ea`), `nameSource`,
  `status` (`busy` | `idle`), `updatedAt`, `statusUpdatedAt`.
  The existence of the file plus a live `pid` is the only true liveness signal
  available. `updatedAt` is **not** a heartbeat — see "Status ladder" below.
- `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl` — transcripts. 35 files.

### Source B — Claude desktop app

- `~/Library/Application Support/Claude/claude-code-sessions/<a>/<b>/local_<uuid>.json`
- `~/Library/Application Support/Claude/local-agent-mode-sessions/<a>/<b>/local_<uuid>.json`

  27 files total. Fields: `sessionId`, `cliSessionId`, `cwd`, `originCwd`,
  `createdAt`, `lastActivityAt`, `lastFocusedAt`, `model`, `effort`, `isArchived`,
  `title` (human-readable, auto-generated), `titleSource`, `permissionMode`.

- Transcripts live in a per-session sandboxed HOME:
  `.../local_<uuid>/.claude/projects/<slug>/<sessionId>.jsonl` — 44 files, byte-identical
  in format to CLI transcripts.

**Note:** `cliSessionId` matches a `~/.claude/projects` transcript for only 1 of 27
desktop sessions. Do not rely on that join. Resolve desktop transcripts via the nested
sandbox path instead.

### Transcript schema (shared by both sources)

Line types observed: `assistant`, `user`, `attachment`, `system`, `last-prompt`,
`mode`, `ai-title`, `permission-mode`, `queue-operation`, `file-history-delta`,
`file-history-snapshot`, `frame-link`.

Fields used by this project: `type`, `timestamp`, `sessionId`, `cwd`, `gitBranch`,
`version`, `isSidechain`, `uuid`, `parentUuid`, `message.model`, `message.usage`,
`message.content[]` (`tool_use` and `tool_result` blocks), `toolUseResult`.

`message.usage` provides `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`.

## Known constraint: sub-agent visibility

Across all 79 transcripts there are **zero** lines with `isSidechain: true`, despite
42 `Agent` and 24 `TaskCreate` tool calls. Sub-agent internal turns are not written to
disk in this version.

Therefore sub-agent tracking is **dispatch-and-return only**, derived from the parent
transcript: a `tool_use` block naming `Agent`/`Task`/`TaskCreate`, matched by
`tool_use.id` to a later `tool_result`. Unmatched means still running.

The UI must not imply live visibility inside a sub-agent.

## Architecture

Single Python 3 process, standard library only (no pip dependencies). Read-only
filesystem access. Serves a browser page that polls a JSON endpoint.

Run: `python3 -m fleet` → `http://localhost:8787`

### Units

| Unit | Responsibility | Depends on |
|---|---|---|
| `fleet/sources.py` | Adapters that enumerate sessions and locate transcripts. Interface: `discover() -> list[RawSession]` | filesystem |
| `fleet/transcript.py` | Parse one `.jsonl` into `TranscriptFacts`. Incremental and cached. | nothing |
| `fleet/model.py` | Merge `RawSession` + `TranscriptFacts` into `AgentCard`; status ladder; sort | sources, transcript |
| `fleet/prices.py` | Editable model → price-per-million-token map | nothing |
| `fleet/server.py` | `http.server`; serves `/` and `/api/fleet` | model |
| `fleet/static/` | UI: `index.html`, `app.css`, `app.js`. Polls `/api/fleet`. | nothing |

`sources.py` contains two adapters, `CliAdapter` and `DesktopAdapter`, behind one
interface. A future third source (e.g. cloud agents) is a new adapter and nothing else.

### Data contract

```
RawSession:
  id, source ("cli"|"app"), name, cwd, transcript_path,
  status_hint, pid, model, effort, started_at, last_activity_at

TranscriptFacts:
  last_tool: {name, detail, at} | None
  tokens: {input, output, cache_creation, cache_read}
  models_seen: [str]
  subagents: [{id, kind, description, dispatched_at, returned_at|None}]
  git_branch, last_line_at, malformed_lines

AgentCard:
  id, source, name, cwd, git_branch, model, status, status_reason,
  last_tool, subagents {running, done, items[]},
  tokens, cost_estimate, started_at, last_activity_at, error|None
```

Name resolution, in order: desktop `title` → CLI derived `name` → `basename(cwd)`.

## Status ladder

### `updatedAt` is not a heartbeat — corrected 2026-08-07

An earlier draft of this document asserted that `~/.claude/sessions/<PID>.json`.`updatedAt`
is a periodic heartbeat, and the ladder aged a session into `stale` after 10 minutes of
"heartbeat" silence. **That premise is wrong.** `updatedAt` is an *activity* timestamp: it
advances when the session does something and then stops. It does not tick on its own.

Measured on this machine, all three processes alive:

```
    pid alive status  updatedAt age   name
  14501   YES   busy          0.2h   agent-action-a0
   7377   YES   idle         13.2h   pictures-63
  77660   YES   idle          2.6h   retro-game-ea
```

Two perfectly live sessions were being rendered `stale` — "no heartbeat for 13h" — and
dimmed to the bottom of the board. Worse, the age check ran *before* the busy/attention
branch, so a session genuinely blocked on a permission prompt for over 10 minutes lost
its `attention` highlight and sank — precisely the case this dashboard exists to surface,
and the one most likely to run long.

**`os.kill(pid, 0)` is the one true liveness signal.** The age of `updatedAt` never
produces `stale`. Genuinely old sessions are handled by the 24h fleet-scope window.

### The ladder

Cards sort by this order, most urgent first.

1. **`attention`** — registry `status == "busy"` but transcript has not grown in
   **>60 seconds**, *and no sub-agent dispatch is outstanding*. Nearly always a
   permission prompt or a question awaiting an answer. This is an **inference**. The card
   must word it as "likely waiting on input" and never assert it as fact. There is no
   expiry: a session blocked for six hours is still `attention`.
2. **`busy`** — busy and actively writing to the transcript, **or** busy, silent, and
   waiting on a sub-agent of its own. Sub-agent turns are never written to the parent's
   transcript, so a dispatching parent looks silent for the whole dispatch; that session
   is working, not waiting on the user, and must not be reported as `attention`.
3. **`idle`** — process alive, awaiting input. However long its last activity was ago.
4. **`stale`** — the process is gone. Either `os.kill(pid, 0)` raises
   `ProcessLookupError`, or the PID is alive but provably belongs to a *different*
   process (see below).

### PID-reuse guard

A live PID proves some process holds that PID, not that it is the one that wrote the
session file; the OS recycles PIDs, and a stale session file could otherwise resurrect
as `busy`. The session's own `startedAt` is compared against the live process's start
time, read from `ps -o etime=` (elapsed time, unlike `lstart`, is locale-independent —
and the session file's `procStart` string is UTC while `ps` prints local time). A genuine
session's process starts 1–5 **seconds before** its `startedAt`; a recycled PID's process
necessarily started after the original died, and so long after it. A process starting more
than 120s after `startedAt` is therefore a different process. The check **fails open**: no
`ps`, or no `startedAt`, means no proof of reuse, and liveness wins. Hiding a live session
is the failure this dashboard cannot afford.

Desktop sessions have no heartbeat and no PID, so the ladder above cannot be applied
directly. Their status derives from `lastActivityAt` recency alone, mapped explicitly:

- `lastActivityAt` within 60s → **`busy`**
- within 24h → **`idle`**
- older than 24h → excluded by fleet scope
- `isArchived: true` → excluded regardless of age

A desktop session can therefore **never** be assigned `attention`. That state requires
a `busy` heartbeat contradicted by transcript silence, and no such heartbeat exists for
this source. This is a real gap, not an oversight: the dashboard cannot tell when a
desktop session is sitting on a permission prompt.

Because the same colored dot means less for desktop cards, the card labels its source
distinctly so the difference is always visible.

## Fleet scope

Show sessions that are alive now, plus any session with activity in the last **24
hours**, rendered dimmed as `stale`. Sessions older than 24h are excluded. This keeps
the board meaningful when nothing is actively running.

## Performance

79 transcripts, largest 26 MB. A full re-scan every 3 seconds is not viable.

`transcript.py` is incremental. It holds a per-file cache entry:

```
{path: {mtime, size, byte_offset, tokens_accum, subagents, last_tool}}
```

On each poll:
- If `mtime` and `size` are unchanged, return the cached facts untouched.
- Otherwise seek to `byte_offset` and read only the appended bytes, folding new
  usage into the running totals and appending new sub-agent events.
- If the file shrank, or its inode changed, discard the entry and re-scan in full.

A partial trailing line (write in progress) is not consumed; `byte_offset` advances
only to the last complete newline.

Cold start pays one full pass. It runs in a background thread so the server answers
immediately and cards populate as they resolve. Steady state reads kilobytes per poll.

UI polls `/api/fleet` every 3 seconds.

## Cost

Token counts are exact, taken from `usage`. Dollar figures are **not** present in the
data and must be computed from `prices.py`.

Therefore:
- Tokens are the primary displayed figure.
- Dollars are shown as a clearly marked estimate.
- `cache_read_input_tokens` is broken out separately. It dominates volume (60.8M in a
  single observed session) and is priced far below input tokens; folding it into a
  single total would overstate cost by roughly an order of magnitude.

`prices.py` is a plain editable dict so prices can be corrected without touching logic.

## Error handling

The dashboard is read-only and must never write to agent state.

- Every session is processed in its own `try/except`. A failure produces a card with
  an `error` field and an error badge; all other cards render normally.
- A transcript that disappears mid-read (session ended) is skipped silently.
- Malformed JSON lines are skipped and counted in `malformed_lines`.
- If an entire adapter throws (e.g. the desktop app directory does not exist), it
  contributes zero sessions and logs once. The other adapter still renders.

## Testing

The pure functions are the testable core.

- **Unit** — `transcript.py` against small fixture `.jsonl` files covering: usage
  accumulation, sub-agent dispatch matched and unmatched, malformed lines, a partial
  trailing line, and incremental append (parse, append bytes, re-parse, assert totals
  equal a full re-scan).
- **Unit** — the status ladder against synthetic timestamps for all four states,
  including the 60s boundary.
- **Unit** — name resolution fallback chain.
- **Smoke** — run the full collector over the real 79 transcripts; assert no
  exceptions and that every card has a valid status and non-negative tokens.
- **Integration** — start the server, `GET /api/fleet`, assert valid JSON matching the
  `AgentCard` contract.

## Runtime and installation

The dashboard is a local web page served by a background Python process. It is not a
native application bundle.

**Server lifecycle** — a `launchd` user agent at
`~/Library/LaunchAgents/com.joekocovsky.agentfleet.plist`:

- `RunAtLoad: true` — starts at login
- `KeepAlive: true` — restarts if it crashes
- Binds `127.0.0.1:8787` only. Never `0.0.0.0`; the page exposes session titles, working
  directories and prompt fragments and must not be reachable from the network.
- `stdout`/`stderr` to `~/Library/Logs/agent-fleet.log`

Shipped as `install.sh` (writes the plist, `launchctl bootstrap`, prints next steps) and
`uninstall.sh` (`launchctl bootout`, removes the plist). Both are idempotent.

**Viewing** — Chrome → ⋮ → Cast, Save & Share → *Install page as app*. This gives the
dashboard its own Dock icon and a chrome-less window that ⌘-Tabs like a native app.
`install.sh` prints these steps on success.

For this to look right as an installed app, the page must provide:
- a `<title>` that reads well as a window title ("Agent Fleet")
- a `<link rel="icon">` favicon, since it becomes the Dock icon
- a `<meta name="theme-color">` for the window chrome

**Port conflict** — if 8787 is taken, the server exits with a clear message naming the
port and the `--port` flag rather than silently binding elsewhere.

## UI

After this spec is approved and the implementation plan exists, the visual layer is
built with the **ui-ux-pro-max** skill against the `AgentCard` contract above.

Layout requirements the UI must honor:
- `attention` cards sort to the top and are visually distinct.
- Source (CLI vs desktop app) is always visible on the card, since it changes how much
  the status can be trusted.
- The activity line shows last tool name, a short detail, and its age.
- Sub-agent rollup is a compact count that expands to descriptions.
- Wording around inferred status stays hedged.

## Open decisions deferred to v2

- Configurable stuck threshold in the UI (ships hard-coded at 60s).
- Historical/archive browsing beyond the 24h window.
- Any form of control (kill, prompt, spawn).
