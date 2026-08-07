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
  This is the only true liveness heartbeat available.
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

Cards sort by this order, most urgent first.

1. **`attention`** — registry `status == "busy"` but transcript has not grown in
   **>60 seconds**. Nearly always a permission prompt or a question awaiting an answer.
   This is an **inference**. The card must word it as "likely waiting on input" and
   never assert it as fact.
2. **`busy`** — busy and actively writing to the transcript.
3. **`idle`** — process alive, awaiting input.
4. **`stale`** — PID no longer exists (`os.kill(pid, 0)` raises `ProcessLookupError`),
   or heartbeat older than 10 minutes.

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
