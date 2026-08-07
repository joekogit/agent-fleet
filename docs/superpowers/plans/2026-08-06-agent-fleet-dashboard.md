# Agent Fleet Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only local web dashboard showing every running Claude agent — CLI and desktop app — with status, current activity, sub-agent dispatches, and token burn.

**Architecture:** A single Python process reads two on-disk session sources plus their `.jsonl` transcripts, normalizes both into one `AgentCard` record, and serves that as JSON to a browser page that polls every 3 seconds. Transcript reading is incremental (byte-offset cached) because the corpus is 79 files and up to 26 MB each. A `launchd` agent keeps the server alive at login.

**Tech Stack:** Python 3.11+ standard library only. `http.server`, `dataclasses`, `json`, `unittest`. No pip packages, no build step. Vanilla HTML/CSS/JS on the front end.

## Global Constraints

- **Python 3.11+**, **standard library only.** No pip installs, no `requirements.txt`. Tests use `unittest`, not pytest.
- **Read-only.** The program must never write, move, or delete anything under `~/.claude/` or `~/Library/Application Support/Claude/`. Its only writes are its own log file.
- **Bind `127.0.0.1` only.** Never `0.0.0.0`. The page exposes session titles, working directories and prompt fragments.
- **All timestamps normalize to epoch seconds (float).** Session JSON uses epoch milliseconds; transcripts use ISO-8601 with `Z`. Convert at the boundary; nothing downstream handles two formats.
- **Never assert inferred status as fact.** The `attention` state is a heuristic. User-facing copy says "likely waiting on input", never "waiting on input".
- **One bad file must never blank the fleet.** Per-session `try/except`; a failure yields a card with `error` set while all other cards render.
- **Spec:** `docs/superpowers/specs/2026-08-06-agent-fleet-dashboard-design.md`

## File Structure

| File | Responsibility |
|---|---|
| `fleet/__init__.py` | Package marker, version constant |
| `fleet/__main__.py` | CLI entry: arg parsing, starts server |
| `fleet/types.py` | All shared dataclasses. Imported by everything; imports nothing from the package (prevents cycles) |
| `fleet/timeutil.py` | Timestamp normalization helpers |
| `fleet/transcript.py` | Parse one `.jsonl` → `TranscriptFacts`; incremental byte-offset cache |
| `fleet/sources.py` | `CliAdapter` + `DesktopAdapter` → `list[RawSession]` |
| `fleet/prices.py` | Editable model→price map; cost estimation |
| `fleet/model.py` | Status ladder, name resolution, `AgentCard` assembly, filter + sort |
| `fleet/server.py` | `ThreadingHTTPServer`, `/api/fleet`, static files |
| `fleet/static/index.html` | Page shell |
| `fleet/static/app.css` | Styles |
| `fleet/static/app.js` | Poll + render |
| `tests/` | `unittest` suite + `.jsonl` fixtures |
| `install.sh` / `uninstall.sh` | launchd agent install/removal |

**Note on `fleet/types.py`:** the spec's unit table did not list it. It is added here so `sources.py` and `model.py` share dataclass definitions without importing each other. This is a refinement, not a scope change.

---

### Task 1: Shared types and time normalization

**Files:**
- Create: `fleet/__init__.py`, `fleet/types.py`, `fleet/timeutil.py`, `tests/__init__.py`
- Test: `tests/test_timeutil.py`

**Interfaces:**
- Consumes: nothing
- Produces: `ToolCall`, `SubagentDispatch`, `TranscriptFacts`, `RawSession`, `AgentCard` dataclasses; `iso_to_epoch(str) -> float | None`; `ms_to_epoch(int | None) -> float | None`

- [ ] **Step 1: Write the failing test**

`tests/test_timeutil.py`:

```python
import unittest
from datetime import datetime, timezone
from fleet.timeutil import iso_to_epoch, ms_to_epoch


class TestIsoToEpoch(unittest.TestCase):
    def test_parses_transcript_format(self):
        # Exact format seen in real transcripts: '2026-08-06T17:22:10.050Z'
        expected = datetime(
            2026, 8, 6, 17, 22, 10, 50000, tzinfo=timezone.utc
        ).timestamp()
        self.assertAlmostEqual(
            iso_to_epoch("2026-08-06T17:22:10.050Z"), expected, places=3
        )

    def test_returns_none_for_garbage(self):
        self.assertIsNone(iso_to_epoch("not a date"))
        self.assertIsNone(iso_to_epoch(None))
        self.assertIsNone(iso_to_epoch(""))


class TestMsToEpoch(unittest.TestCase):
    def test_converts_milliseconds(self):
        self.assertEqual(ms_to_epoch(1786038157970), 1786038157.970)

    def test_returns_none_for_none(self):
        self.assertIsNone(ms_to_epoch(None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_timeutil -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet'`

- [ ] **Step 3: Write the implementation**

`fleet/__init__.py`:

```python
__version__ = "0.1.0"
```

`tests/__init__.py`: (empty file)

`fleet/timeutil.py`:

```python
"""Timestamp normalization. Everything in this program is epoch seconds (float)."""
from datetime import datetime, timezone


def iso_to_epoch(value):
    """Parse a transcript ISO-8601 timestamp ('2026-08-06T17:22:10.050Z')."""
    if not value or not isinstance(value, str):
        return None
    try:
        # fromisoformat handles '+00:00' but not the 'Z' suffix before 3.11
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ms_to_epoch(value):
    """Convert session-JSON epoch milliseconds to epoch seconds."""
    if value is None or not isinstance(value, (int, float)):
        return None
    return value / 1000.0


def utc_now():
    return datetime.now(timezone.utc).timestamp()
```

`fleet/types.py`:

```python
"""Shared record types. Imports nothing else from this package."""
from dataclasses import dataclass, field
from typing import Optional, List, Dict


@dataclass(frozen=True)
class ToolCall:
    name: str
    detail: str
    at: Optional[float]


@dataclass
class SubagentDispatch:
    id: str
    kind: str                      # "Agent" | "Task" | "TaskCreate"
    description: str
    dispatched_at: Optional[float]
    returned_at: Optional[float] = None


@dataclass
class TranscriptFacts:
    last_tool: Optional[ToolCall] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    models_seen: List[str] = field(default_factory=list)
    subagents: List[SubagentDispatch] = field(default_factory=list)
    git_branch: Optional[str] = None
    last_line_at: Optional[float] = None
    malformed_lines: int = 0


@dataclass(frozen=True)
class RawSession:
    id: str
    source: str                    # "cli" | "app"
    name: str
    cwd: str
    transcript_path: Optional[str]
    status_hint: Optional[str]     # "busy" | "idle" | None
    pid: Optional[int]
    model: Optional[str]
    effort: Optional[str]
    started_at: Optional[float]
    last_activity_at: Optional[float]


@dataclass
class AgentCard:
    id: str
    source: str
    name: str
    cwd: str
    git_branch: Optional[str]
    model: Optional[str]
    status: str                    # "attention" | "busy" | "idle" | "stale"
    status_reason: str
    last_tool: Optional[Dict]
    subagents_running: int
    subagents_done: int
    subagents: List[Dict]
    tokens: Dict[str, int]
    cost_estimate: Optional[float]
    started_at: Optional[float]
    last_activity_at: Optional[float]
    error: Optional[str] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_timeutil -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add fleet/__init__.py fleet/types.py fleet/timeutil.py tests/__init__.py tests/test_timeutil.py
git commit -m "feat: add shared types and timestamp normalization"
```

---

### Task 2: Transcript parsing (full scan)

**Files:**
- Create: `fleet/transcript.py`, `tests/fixtures/basic.jsonl`, `tests/fixtures/subagents.jsonl`, `tests/fixtures/malformed.jsonl`
- Test: `tests/test_transcript.py`

**Interfaces:**
- Consumes: `fleet.types.{ToolCall, SubagentDispatch, TranscriptFacts}`, `fleet.timeutil.iso_to_epoch`
- Produces: `parse_lines(lines: Iterable[str], facts: TranscriptFacts) -> TranscriptFacts` (mutates and returns `facts`, so Task 3 can fold appended bytes into an existing accumulator); `tool_detail(name: str, tool_input: dict) -> str`

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/basic.jsonl` — three lines, no trailing newline issues:

```
{"type":"assistant","timestamp":"2026-08-06T17:00:00.000Z","gitBranch":"main","message":{"model":"claude-opus-5","usage":{"input_tokens":10,"output_tokens":20,"cache_creation_input_tokens":5,"cache_read_input_tokens":100},"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"ls -la","description":"List files"}}]}}
{"type":"user","timestamp":"2026-08-06T17:00:01.000Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_1","content":"ok"}]}}
{"type":"assistant","timestamp":"2026-08-06T17:00:05.000Z","gitBranch":"main","message":{"model":"claude-opus-5","usage":{"input_tokens":3,"output_tokens":7,"cache_creation_input_tokens":0,"cache_read_input_tokens":50},"content":[{"type":"tool_use","id":"toolu_2","name":"Read","input":{"file_path":"/tmp/x/notes.md"}}]}}
```

`tests/fixtures/subagents.jsonl` — one returned, one still running:

```
{"type":"assistant","timestamp":"2026-08-06T18:00:00.000Z","message":{"model":"claude-opus-5","content":[{"type":"tool_use","id":"toolu_a","name":"Agent","input":{"description":"Audit auth code","subagent_type":"Explore"}}]}}
{"type":"assistant","timestamp":"2026-08-06T18:00:01.000Z","message":{"model":"claude-opus-5","content":[{"type":"tool_use","id":"toolu_b","name":"Agent","input":{"description":"Write migration","subagent_type":"general-purpose"}}]}}
{"type":"user","timestamp":"2026-08-06T18:04:00.000Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_a","content":"done"}]}}
```

`tests/fixtures/malformed.jsonl` — valid, garbage, valid:

```
{"type":"assistant","timestamp":"2026-08-06T19:00:00.000Z","message":{"model":"claude-opus-5","usage":{"input_tokens":1,"output_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0},"content":[]}}
{"type":"assistant","timestamp":  BROKEN NOT JSON
{"type":"assistant","timestamp":"2026-08-06T19:00:02.000Z","message":{"model":"claude-opus-5","usage":{"input_tokens":2,"output_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":0},"content":[]}}
```

- [ ] **Step 2: Write the failing test**

`tests/test_transcript.py`:

```python
import os
import unittest
from fleet.types import TranscriptFacts
from fleet.transcript import parse_lines, tool_detail

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return parse_lines(fh, TranscriptFacts())


class TestTokenAccumulation(unittest.TestCase):
    def test_sums_all_four_token_kinds(self):
        f = load("basic.jsonl")
        self.assertEqual(f.input_tokens, 13)
        self.assertEqual(f.output_tokens, 27)
        self.assertEqual(f.cache_creation_tokens, 5)
        self.assertEqual(f.cache_read_tokens, 150)

    def test_records_model_once(self):
        f = load("basic.jsonl")
        self.assertEqual(f.models_seen, ["claude-opus-5"])

    def test_captures_git_branch(self):
        self.assertEqual(load("basic.jsonl").git_branch, "main")


class TestLastTool(unittest.TestCase):
    def test_last_tool_is_the_final_tool_use(self):
        f = load("basic.jsonl")
        self.assertEqual(f.last_tool.name, "Read")
        self.assertEqual(f.last_tool.detail, "notes.md")

    def test_bash_detail_prefers_description(self):
        self.assertEqual(
            tool_detail("Bash", {"command": "ls -la", "description": "List files"}),
            "List files",
        )

    def test_bash_detail_falls_back_to_command(self):
        self.assertEqual(tool_detail("Bash", {"command": "ls -la"}), "ls -la")

    def test_unknown_tool_detail_is_empty_not_crash(self):
        self.assertEqual(tool_detail("Mystery", {}), "")


class TestSubagents(unittest.TestCase):
    def test_matches_results_to_dispatches(self):
        f = load("subagents.jsonl")
        self.assertEqual(len(f.subagents), 2)
        by_id = {s.id: s for s in f.subagents}
        self.assertIsNotNone(by_id["toolu_a"].returned_at)
        self.assertIsNone(by_id["toolu_b"].returned_at)

    def test_keeps_description(self):
        by_id = {s.id: s for s in load("subagents.jsonl").subagents}
        self.assertEqual(by_id["toolu_a"].description, "Audit auth code")

    def test_agent_dispatch_is_not_recorded_as_last_tool(self):
        # An Agent call is a sub-agent event, but it IS still a tool call.
        # It should appear as last_tool too — assert we did not drop it.
        self.assertEqual(load("subagents.jsonl").last_tool.name, "Agent")


class TestMalformed(unittest.TestCase):
    def test_skips_bad_lines_and_counts_them(self):
        f = load("malformed.jsonl")
        self.assertEqual(f.malformed_lines, 1)
        self.assertEqual(f.input_tokens, 3)  # 1 + 2, bad line skipped


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m unittest tests.test_transcript -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.transcript'`

- [ ] **Step 4: Write the implementation**

`fleet/transcript.py`:

```python
"""Parse Claude transcript .jsonl files into TranscriptFacts."""
import json
import os

from .types import ToolCall, SubagentDispatch, TranscriptFacts
from .timeutil import iso_to_epoch

SUBAGENT_TOOLS = ("Agent", "Task", "TaskCreate")
_MAX_DETAIL = 90


def tool_detail(name, tool_input):
    """A short human-readable hint about what a tool call is doing."""
    if not isinstance(tool_input, dict):
        return ""
    if name == "Bash":
        value = tool_input.get("description") or tool_input.get("command") or ""
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        path = tool_input.get("file_path") or ""
        value = os.path.basename(path) or path
    elif name in SUBAGENT_TOOLS:
        value = tool_input.get("description") or tool_input.get("subagent_type") or ""
    elif name == "Skill":
        value = tool_input.get("skill") or ""
    else:
        value = tool_input.get("description") or tool_input.get("query") or ""
    value = str(value).replace("\n", " ").strip()
    return value[:_MAX_DETAIL]


def _subagent_description(tool_input):
    if not isinstance(tool_input, dict):
        return ""
    text = tool_input.get("description") or tool_input.get("prompt") or ""
    return str(text).replace("\n", " ").strip()[:_MAX_DETAIL]


def parse_lines(lines, facts):
    """Fold JSONL lines into `facts`, mutating and returning it.

    Accepts any iterable of strings so Task 3 can feed it only appended bytes.
    """
    pending = {s.id: s for s in facts.subagents if s.returned_at is None}

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            facts.malformed_lines += 1
            continue
        if not isinstance(record, dict):
            facts.malformed_lines += 1
            continue

        at = iso_to_epoch(record.get("timestamp"))
        if at is not None:
            if facts.last_line_at is None or at > facts.last_line_at:
                facts.last_line_at = at

        if record.get("gitBranch"):
            facts.git_branch = record["gitBranch"]

        message = record.get("message")
        if not isinstance(message, dict):
            continue

        model = message.get("model")
        if model and model not in facts.models_seen:
            facts.models_seen.append(model)

        usage = message.get("usage")
        if isinstance(usage, dict):
            facts.input_tokens += _int(usage.get("input_tokens"))
            facts.output_tokens += _int(usage.get("output_tokens"))
            facts.cache_creation_tokens += _int(usage.get("cache_creation_input_tokens"))
            facts.cache_read_tokens += _int(usage.get("cache_read_input_tokens"))

        content = message.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "tool_use":
                name = block.get("name") or "?"
                facts.last_tool = ToolCall(
                    name=name, detail=tool_detail(name, block.get("input")), at=at
                )
                if name in SUBAGENT_TOOLS:
                    dispatch = SubagentDispatch(
                        id=block.get("id") or "",
                        kind=name,
                        description=_subagent_description(block.get("input")),
                        dispatched_at=at,
                    )
                    facts.subagents.append(dispatch)
                    pending[dispatch.id] = dispatch

            elif btype == "tool_result":
                dispatch = pending.pop(block.get("tool_use_id"), None)
                if dispatch is not None:
                    dispatch.returned_at = at

    return facts


def _int(value):
    return value if isinstance(value, int) else 0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest tests.test_transcript -v`
Expected: PASS, 10 tests

- [ ] **Step 6: Commit**

```bash
git add fleet/transcript.py tests/test_transcript.py tests/fixtures
git commit -m "feat: parse transcripts into tokens, tools, and subagent dispatches"
```

---

### Task 3: Incremental transcript cache

**Files:**
- Modify: `fleet/transcript.py` (append `TranscriptCache`)
- Test: `tests/test_incremental.py`

**Interfaces:**
- Consumes: `parse_lines`, `TranscriptFacts`
- Produces: `TranscriptCache` with `facts_for(path: str) -> TranscriptFacts`

This is the load-bearing performance unit. 79 files, largest 26 MB, polled every 3s.

- [ ] **Step 1: Write the failing test**

`tests/test_incremental.py`:

```python
import os
import tempfile
import unittest
from fleet.transcript import TranscriptCache, parse_lines
from fleet.types import TranscriptFacts

LINE_A = '{"type":"assistant","timestamp":"2026-08-06T17:00:00.000Z","message":{"model":"m","usage":{"input_tokens":10,"output_tokens":1,"cache_creation_input_tokens":0,"cache_read_input_tokens":0},"content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"description":"first"}}]}}\n'
LINE_B = '{"type":"assistant","timestamp":"2026-08-06T17:00:10.000Z","message":{"model":"m","usage":{"input_tokens":5,"output_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":0},"content":[{"type":"tool_use","id":"t2","name":"Read","input":{"file_path":"/a/b.py"}}]}}\n'


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.jsonl")

    def write(self, text, mode="w"):
        with open(self.path, mode) as fh:
            fh.write(text)

    def test_append_matches_full_rescan(self):
        self.write(LINE_A)
        cache = TranscriptCache()
        cache.facts_for(self.path)
        self.write(LINE_B, mode="a")
        incremental = cache.facts_for(self.path)

        with open(self.path) as fh:
            full = parse_lines(fh, TranscriptFacts())

        self.assertEqual(incremental.input_tokens, full.input_tokens)
        self.assertEqual(incremental.output_tokens, full.output_tokens)
        self.assertEqual(incremental.last_tool.name, full.last_tool.name)

    def test_unchanged_file_is_not_reread(self):
        self.write(LINE_A)
        cache = TranscriptCache()
        first = cache.facts_for(self.path)
        second = cache.facts_for(self.path)
        self.assertIs(first, second)          # same object, no re-parse
        self.assertEqual(second.input_tokens, 10)

    def test_partial_trailing_line_is_not_consumed(self):
        self.write(LINE_A + '{"type":"assistant","timestamp":"2026')
        cache = TranscriptCache()
        facts = cache.facts_for(self.path)
        self.assertEqual(facts.input_tokens, 10)
        self.assertEqual(facts.malformed_lines, 0)   # partial != malformed

        # Complete the line; it must now be counted exactly once.
        self.write('-08-06T17:00:10.000Z","message":{"model":"m","usage":{"input_tokens":5,"output_tokens":2,"cache_creation_input_tokens":0,"cache_read_input_tokens":0},"content":[]}}\n', mode="a")
        facts = cache.facts_for(self.path)
        self.assertEqual(facts.input_tokens, 15)
        self.assertEqual(facts.malformed_lines, 0)

    def test_truncated_file_resets(self):
        self.write(LINE_A + LINE_B)
        cache = TranscriptCache()
        cache.facts_for(self.path)
        self.write(LINE_A)                     # file shrank
        facts = cache.facts_for(self.path)
        self.assertEqual(facts.input_tokens, 10)

    def test_missing_file_returns_none(self):
        self.assertIsNone(TranscriptCache().facts_for(os.path.join(self.dir, "nope.jsonl")))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_incremental -v`
Expected: FAIL — `ImportError: cannot import name 'TranscriptCache'`

- [ ] **Step 3: Append the implementation to `fleet/transcript.py`**

```python
class _Entry:
    __slots__ = ("inode", "mtime", "size", "offset", "facts")

    def __init__(self, inode):
        self.inode = inode
        self.mtime = None
        self.size = 0
        self.offset = 0
        self.facts = TranscriptFacts()


class TranscriptCache:
    """Reads only the bytes appended since the last poll.

    A full re-scan of every transcript on a 3s interval is not viable at this
    corpus size, so each file keeps a byte offset. A partial trailing line (a
    write in progress) is never consumed: the offset advances only to the last
    complete newline.
    """

    def __init__(self):
        self._entries = {}

    def facts_for(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            self._entries.pop(path, None)
            return None

        entry = self._entries.get(path)
        rotated = (
            entry is None
            or entry.inode != stat.st_ino
            or stat.st_size < entry.size          # truncated or rewritten
        )
        if rotated:
            entry = _Entry(stat.st_ino)
            self._entries[path] = entry
        elif stat.st_size == entry.size and stat.st_mtime == entry.mtime:
            return entry.facts                    # untouched — no read at all

        try:
            with open(path, "rb") as fh:
                fh.seek(entry.offset)
                chunk = fh.read()
        except OSError:
            return entry.facts

        consumed = chunk.rfind(b"\n")
        if consumed == -1:
            # No complete line yet. Leave offset alone and wait.
            entry.mtime = stat.st_mtime
            entry.size = stat.st_size
            return entry.facts

        complete = chunk[: consumed + 1]
        entry.offset += len(complete)
        entry.mtime = stat.st_mtime
        entry.size = stat.st_size

        text = complete.decode("utf-8", errors="replace")
        parse_lines(text.splitlines(), entry.facts)
        return entry.facts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_incremental -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Run the whole suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS, all tests from Tasks 1–3

- [ ] **Step 6: Commit**

```bash
git add fleet/transcript.py tests/test_incremental.py
git commit -m "feat: incremental byte-offset transcript cache"
```

---

### Task 4: Source adapters

**Files:**
- Create: `fleet/sources.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes: `fleet.types.RawSession`, `fleet.timeutil.ms_to_epoch`
- Produces: `CliAdapter(home: str)` and `DesktopAdapter(app_support: str)`, each with `.discover() -> list[RawSession]`; module function `discover_all(home=None, app_support=None) -> list[RawSession]`

Both adapters take their root directory as a constructor argument so tests can point them at a temp tree. Defaults resolve to the real locations.

- [ ] **Step 1: Write the failing test**

`tests/test_sources.py`:

```python
import json
import os
import tempfile
import unittest
from fleet.sources import CliAdapter, DesktopAdapter


class CliAdapterTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude", "sessions"))
        self.proj = os.path.join(self.home, ".claude", "projects", "-tmp-demo")
        os.makedirs(self.proj)

    def add_session(self, pid, session_id, status="idle", name="demo-a0"):
        payload = {
            "pid": pid, "sessionId": session_id, "cwd": "/tmp/demo",
            "startedAt": 1786038157970, "version": "2.1.223", "kind": "interactive",
            "name": name, "status": status, "updatedAt": 1786038213585,
        }
        path = os.path.join(self.home, ".claude", "sessions", f"{pid}.json")
        with open(path, "w") as fh:
            json.dump(payload, fh)

    def test_reads_session_registry(self):
        self.add_session(999, "sess-1", status="busy")
        sessions = CliAdapter(self.home).discover()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s.source, "cli")
        self.assertEqual(s.pid, 999)
        self.assertEqual(s.name, "demo-a0")
        self.assertEqual(s.status_hint, "busy")
        self.assertEqual(s.cwd, "/tmp/demo")

    def test_locates_transcript_by_session_id(self):
        self.add_session(999, "sess-1")
        open(os.path.join(self.proj, "sess-1.jsonl"), "w").close()
        s = CliAdapter(self.home).discover()[0]
        self.assertTrue(s.transcript_path.endswith("sess-1.jsonl"))

    def test_missing_transcript_is_none_not_error(self):
        self.add_session(999, "sess-nope")
        self.assertIsNone(CliAdapter(self.home).discover()[0].transcript_path)

    def test_corrupt_session_file_is_skipped(self):
        self.add_session(999, "sess-1")
        with open(os.path.join(self.home, ".claude", "sessions", "bad.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(len(CliAdapter(self.home).discover()), 1)

    def test_missing_root_yields_nothing(self):
        self.assertEqual(CliAdapter("/nonexistent/path").discover(), [])


class DesktopAdapterTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sess_dir = os.path.join(
            self.root, "claude-code-sessions", "acct", "wksp"
        )
        os.makedirs(self.sess_dir)

    def add(self, uuid, title="Morning brief", archived=False):
        payload = {
            "sessionId": f"local_{uuid}", "cliSessionId": "abc",
            "cwd": "/Users/joe", "createdAt": 1785696825173,
            "lastActivityAt": 1785696840358, "model": "claude-opus-5",
            "effort": "high", "isArchived": archived, "title": title,
        }
        with open(os.path.join(self.sess_dir, f"local_{uuid}.json"), "w") as fh:
            json.dump(payload, fh)

    def test_uses_title_as_name(self):
        self.add("u1")
        sessions = DesktopAdapter(self.root).discover()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].name, "Morning brief")
        self.assertEqual(sessions[0].source, "app")
        self.assertIsNone(sessions[0].pid)
        self.assertIsNone(sessions[0].status_hint)

    def test_skips_archived(self):
        self.add("u1", archived=True)
        self.assertEqual(DesktopAdapter(self.root).discover(), [])

    def test_finds_transcript_in_sandboxed_home(self):
        self.add("u1")
        # Real layout: local_<uuid>/.claude/projects/<slug>/<sessionId>.jsonl
        nested = os.path.join(
            self.sess_dir, "local_u1", ".claude", "projects", "-out-abc"
        )
        os.makedirs(nested)
        open(os.path.join(nested, "deadbeef.jsonl"), "w").close()
        s = DesktopAdapter(self.root).discover()[0]
        self.assertTrue(s.transcript_path.endswith("deadbeef.jsonl"))

    def test_no_sandbox_dir_is_none_not_error(self):
        self.add("u1")
        self.assertIsNone(DesktopAdapter(self.root).discover()[0].transcript_path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_sources -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.sources'`

- [ ] **Step 3: Write the implementation**

`fleet/sources.py`:

```python
"""Discover agent sessions from the CLI and the desktop app.

Each adapter answers one question: what sessions exist, and where is each
one's transcript? Neither reads transcript contents.
"""
import glob
import json
import os

from .types import RawSession
from .timeutil import ms_to_epoch

DEFAULT_HOME = os.path.expanduser("~")
DEFAULT_APP_SUPPORT = os.path.expanduser(
    "~/Library/Application Support/Claude"
)
# The desktop app keeps sessions under two sibling trees.
DESKTOP_TREES = ("claude-code-sessions", "local-agent-mode-sessions")


def _load_json(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _newest(paths):
    newest, newest_mtime = None, -1.0
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest


class CliAdapter:
    """~/.claude/sessions/<PID>.json plus ~/.claude/projects/**/<sessionId>.jsonl"""

    source = "cli"

    def __init__(self, home=None):
        self.home = home or DEFAULT_HOME

    def discover(self):
        sessions_dir = os.path.join(self.home, ".claude", "sessions")
        projects_dir = os.path.join(self.home, ".claude", "projects")
        out = []
        for path in sorted(glob.glob(os.path.join(sessions_dir, "*.json"))):
            data = _load_json(path)
            if not data or not data.get("sessionId"):
                continue
            session_id = data["sessionId"]
            transcript = _newest(
                glob.glob(os.path.join(projects_dir, "*", f"{session_id}.jsonl"))
            )
            cwd = data.get("cwd") or ""
            out.append(
                RawSession(
                    id=session_id,
                    source=self.source,
                    name=data.get("name") or os.path.basename(cwd) or session_id[:8],
                    cwd=cwd,
                    transcript_path=transcript,
                    status_hint=data.get("status"),
                    pid=data.get("pid"),
                    model=None,          # not recorded here; transcript supplies it
                    effort=None,
                    started_at=ms_to_epoch(data.get("startedAt")),
                    last_activity_at=ms_to_epoch(data.get("updatedAt")),
                )
            )
        return out


class DesktopAdapter:
    """Desktop app sessions, whose transcripts live in a sandboxed HOME.

    `cliSessionId` looks like a join key into ~/.claude/projects but resolves
    for only 1 of 27 observed sessions. Do not use it. The transcript is found
    inside the session's own sandbox directory instead.
    """

    source = "app"

    def __init__(self, app_support=None):
        self.app_support = app_support or DEFAULT_APP_SUPPORT

    def discover(self):
        out = []
        for tree in DESKTOP_TREES:
            pattern = os.path.join(self.app_support, tree, "*", "*", "local_*.json")
            for path in sorted(glob.glob(pattern)):
                data = _load_json(path)
                if not data or not data.get("sessionId"):
                    continue
                if data.get("isArchived"):
                    continue
                cwd = data.get("cwd") or ""
                out.append(
                    RawSession(
                        id=data["sessionId"],
                        source=self.source,
                        name=data.get("title")
                        or os.path.basename(cwd)
                        or data["sessionId"][:14],
                        cwd=cwd,
                        transcript_path=self._transcript_for(path),
                        status_hint=None,      # no heartbeat exists for this source
                        pid=None,
                        model=data.get("model"),
                        effort=data.get("effort"),
                        started_at=ms_to_epoch(data.get("createdAt")),
                        last_activity_at=ms_to_epoch(data.get("lastActivityAt")),
                    )
                )
        return out

    def _transcript_for(self, session_json_path):
        sandbox = session_json_path[: -len(".json")]
        return _newest(
            glob.glob(
                os.path.join(sandbox, ".claude", "projects", "*", "*.jsonl")
            )
        )


def discover_all(home=None, app_support=None):
    """Every session from every adapter. One failing adapter never kills the rest."""
    sessions = []
    for adapter in (CliAdapter(home), DesktopAdapter(app_support)):
        try:
            sessions.extend(adapter.discover())
        except Exception:
            continue
    return sessions
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_sources -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Sanity-check against the real machine**

Run:

```bash
python3 -c "
from fleet.sources import discover_all
for s in discover_all():
    print(f'{s.source:4s} {s.name[:30]:30s} tx={bool(s.transcript_path)} status={s.status_hint}')
"
```

Expected: several `cli` rows (one per running Claude process) and a set of `app` rows with human titles. Most should show `tx=True`. If every `app` row shows `tx=False`, the sandbox glob is wrong — fix before continuing.

- [ ] **Step 6: Commit**

```bash
git add fleet/sources.py tests/test_sources.py
git commit -m "feat: discover CLI and desktop app sessions"
```

---

### Task 5: Pricing and cost estimation

**Files:**
- Create: `fleet/prices.py`
- Test: `tests/test_prices.py`

**Interfaces:**
- Consumes: nothing
- Produces: `PRICES: dict[str, ModelPrice]`, `estimate_cost(model: str | None, tokens: dict) -> float | None`

Dollar figures do not exist in the data. They are computed from a rate table the user edits. An unknown model returns `None`, not a wrong number — the UI renders `—`.

- [ ] **Step 1: Write the failing test**

`tests/test_prices.py`:

```python
import unittest
from fleet.prices import estimate_cost, PRICES, ModelPrice

TOKENS = {
    "input": 1_000_000,
    "output": 1_000_000,
    "cache_creation": 0,
    "cache_read": 0,
}


class EstimateCostTest(unittest.TestCase):
    def test_known_model_sums_input_and_output(self):
        PRICES["test-model"] = ModelPrice(
            input=10.0, output=20.0, cache_write=0.0, cache_read=0.0
        )
        self.assertAlmostEqual(estimate_cost("test-model", TOKENS), 30.0)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(estimate_cost("no-such-model", TOKENS))

    def test_none_model_returns_none(self):
        self.assertIsNone(estimate_cost(None, TOKENS))

    def test_cache_read_priced_separately_from_input(self):
        PRICES["cheap-cache"] = ModelPrice(
            input=10.0, output=0.0, cache_write=0.0, cache_read=1.0
        )
        tokens = {"input": 0, "output": 0, "cache_creation": 0,
                  "cache_read": 1_000_000}
        # Priced as cache_read (1.0), NOT as input (10.0).
        self.assertAlmostEqual(estimate_cost("cheap-cache", tokens), 1.0)

    def test_every_shipped_price_is_complete(self):
        for name, price in PRICES.items():
            if name.startswith(("test-", "cheap-")):
                continue
            for field in ("input", "output", "cache_write", "cache_read"):
                self.assertIsInstance(getattr(price, field), float, name)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_prices -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.prices'`

- [ ] **Step 3: Write the implementation**

`fleet/prices.py`:

```python
"""Model pricing.

The transcripts record token counts but no dollar amounts, so every cost shown
by this dashboard is an ESTIMATE derived from the table below.

>>> EDIT THESE RATES. <<<
They are starting values, in US dollars per million tokens, and they go stale
whenever pricing changes. Verify them against current published pricing before
trusting any number the dashboard reports.

An unrecognised model yields None rather than a plausible-looking wrong figure.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    input: float          # $ per 1M input tokens
    output: float         # $ per 1M output tokens
    cache_write: float    # $ per 1M cache-creation tokens
    cache_read: float     # $ per 1M cache-read tokens


PRICES = {
    "claude-opus-5": ModelPrice(
        input=15.0, output=75.0, cache_write=18.75, cache_read=1.50
    ),
    "claude-sonnet-5": ModelPrice(
        input=3.0, output=15.0, cache_write=3.75, cache_read=0.30
    ),
    "claude-fable-5": ModelPrice(
        input=3.0, output=15.0, cache_write=3.75, cache_read=0.30
    ),
    "claude-haiku-4-5-20251001": ModelPrice(
        input=1.0, output=5.0, cache_write=1.25, cache_read=0.10
    ),
}

_PER_MILLION = 1_000_000.0


def estimate_cost(model, tokens):
    """Estimated dollars for one session, or None if the model is unpriced."""
    price = PRICES.get(model)
    if price is None:
        return None
    return (
        tokens.get("input", 0) * price.input
        + tokens.get("output", 0) * price.output
        + tokens.get("cache_creation", 0) * price.cache_write
        + tokens.get("cache_read", 0) * price.cache_read
    ) / _PER_MILLION
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_prices -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add fleet/prices.py tests/test_prices.py
git commit -m "feat: add editable model price table and cost estimation"
```

---

### Task 6: Status ladder and card assembly

**Files:**
- Create: `fleet/model.py`
- Test: `tests/test_model.py`

**Interfaces:**
- Consumes: `RawSession`, `TranscriptFacts`, `AgentCard`, `TranscriptCache`, `discover_all`, `estimate_cost`
- Produces: `classify(raw, facts, now, stuck_after=60.0, stale_after=600.0) -> (status, reason)`; `build_card(raw, facts, now) -> AgentCard`; `Collector(home=None, app_support=None)` with `.snapshot(now=None) -> dict`

Constants: `STUCK_AFTER = 60.0`, `STALE_AFTER = 600.0`, `SCOPE_WINDOW = 86400.0`, `STATUS_ORDER = {"attention": 0, "busy": 1, "idle": 2, "stale": 3}`.

- [ ] **Step 1: Write the failing test**

`tests/test_model.py`:

```python
import unittest
from fleet.types import RawSession, TranscriptFacts, ToolCall, SubagentDispatch
from fleet.model import classify, build_card, sort_cards, in_scope, STUCK_AFTER

NOW = 1_800_000_000.0


def cli(status="busy", last_activity=NOW, pid=None):
    return RawSession(
        id="s1", source="cli", name="demo-a0", cwd="/tmp/demo",
        transcript_path="/tmp/x.jsonl", status_hint=status, pid=pid,
        model=None, effort=None, started_at=NOW - 3600,
        last_activity_at=last_activity,
    )


def app(last_activity=NOW):
    return RawSession(
        id="local_1", source="app", name="Morning brief", cwd="/Users/joe",
        transcript_path=None, status_hint=None, pid=None,
        model="claude-opus-5", effort="high", started_at=NOW - 3600,
        last_activity_at=last_activity,
    )


def facts(last_line_at=NOW):
    return TranscriptFacts(last_line_at=last_line_at)


class ClassifyCliTest(unittest.TestCase):
    """PID None means we cannot prove liveness; treat as not-alive."""

    def test_busy_and_writing_is_busy(self):
        status, _ = classify(cli(status="busy", pid=os_pid()), facts(NOW - 5), NOW)
        self.assertEqual(status, "busy")

    def test_busy_but_silent_is_attention(self):
        status, reason = classify(
            cli(status="busy", pid=os_pid()), facts(NOW - STUCK_AFTER - 1), NOW
        )
        self.assertEqual(status, "attention")
        self.assertIn("likely", reason.lower())

    def test_boundary_at_exactly_stuck_after_is_still_busy(self):
        status, _ = classify(
            cli(status="busy", pid=os_pid()), facts(NOW - STUCK_AFTER), NOW
        )
        self.assertEqual(status, "busy")

    def test_idle_process_is_idle(self):
        status, _ = classify(cli(status="idle", pid=os_pid()), facts(NOW - 5), NOW)
        self.assertEqual(status, "idle")

    def test_dead_pid_is_stale(self):
        status, reason = classify(cli(status="busy", pid=999_999), facts(), NOW)
        self.assertEqual(status, "stale")
        self.assertIn("process", reason.lower())

    def test_alive_pid_with_old_heartbeat_is_stale(self):
        # Guards against PID reuse making a dead session look alive.
        status, _ = classify(
            cli(status="busy", pid=os_pid(), last_activity=NOW - 3600), facts(), NOW
        )
        self.assertEqual(status, "stale")


class ClassifyAppTest(unittest.TestCase):
    def test_recent_activity_is_busy(self):
        self.assertEqual(classify(app(NOW - 10), facts(), NOW)[0], "busy")

    def test_within_a_day_is_idle(self):
        self.assertEqual(classify(app(NOW - 7200), facts(), NOW)[0], "idle")

    def test_older_than_a_day_is_stale(self):
        self.assertEqual(classify(app(NOW - 90_000), facts(), NOW)[0], "stale")

    def test_app_can_never_be_attention(self):
        """No heartbeat exists for this source, so the heuristic cannot apply."""
        for age in (0, 30, 61, 600, 7200):
            self.assertNotEqual(classify(app(NOW - age), facts(), NOW)[0], "attention")


class BuildCardTest(unittest.TestCase):
    def test_rolls_up_subagents(self):
        f = facts()
        f.subagents = [
            SubagentDispatch("a", "Agent", "one", NOW - 60, returned_at=NOW - 10),
            SubagentDispatch("b", "Agent", "two", NOW - 30, returned_at=None),
        ]
        card = build_card(cli(pid=os_pid()), f, NOW)
        self.assertEqual(card.subagents_running, 1)
        self.assertEqual(card.subagents_done, 1)

    def test_model_comes_from_transcript_when_source_lacks_it(self):
        f = facts()
        f.models_seen = ["claude-opus-5"]
        self.assertEqual(build_card(cli(pid=os_pid()), f, NOW).model, "claude-opus-5")

    def test_missing_transcript_yields_card_not_crash(self):
        card = build_card(cli(pid=os_pid()), None, NOW)
        self.assertEqual(card.tokens["input"], 0)
        self.assertIsNone(card.last_tool)

    def test_serializes_last_tool_as_dict(self):
        f = facts()
        f.last_tool = ToolCall(name="Bash", detail="List files", at=NOW - 3)
        card = build_card(cli(pid=os_pid()), f, NOW)
        self.assertEqual(card.last_tool["name"], "Bash")
        self.assertEqual(card.last_tool["detail"], "List files")


class SortAndScopeTest(unittest.TestCase):
    def test_attention_sorts_above_everything(self):
        cards = [
            build_card(cli(status="idle", pid=os_pid()), facts(), NOW),
            build_card(cli(status="busy", pid=os_pid()),
                       facts(NOW - STUCK_AFTER - 5), NOW),
        ]
        self.assertEqual(sort_cards(cards)[0].status, "attention")

    def test_scope_excludes_sessions_older_than_a_day(self):
        self.assertFalse(in_scope(build_card(app(NOW - 90_000), facts(), NOW), NOW))
        self.assertTrue(in_scope(build_card(app(NOW - 3600), facts(), NOW), NOW))


def os_pid():
    import os
    return os.getpid()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_model -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.model'`

- [ ] **Step 3: Write the implementation**

`fleet/model.py`:

```python
"""Normalize sessions from any source into one AgentCard, and rank them."""
import os
import time

from .types import AgentCard, TranscriptFacts
from .transcript import TranscriptCache
from .sources import discover_all
from .prices import estimate_cost

STUCK_AFTER = 60.0        # busy + transcript silence past this => likely blocked
STALE_AFTER = 600.0       # heartbeat older than this => stale
SCOPE_WINDOW = 86400.0    # show live sessions plus anything active in 24h
APP_BUSY_WINDOW = 60.0    # desktop has no heartbeat; recent activity == busy

STATUS_ORDER = {"attention": 0, "busy": 1, "idle": 2, "stale": 3}


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except OSError:
        return False
    return True


def _ago(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def classify(raw, facts, now, stuck_after=STUCK_AFTER, stale_after=STALE_AFTER):
    """Return (status, human reason).

    The 'attention' state is INFERRED, never observed. Its reason string must
    stay hedged — the dashboard cannot actually see a permission prompt.
    """
    if raw.source == "cli":
        if not _pid_alive(raw.pid):
            return "stale", "process is gone"
        heartbeat_age = now - (raw.last_activity_at or 0)
        if heartbeat_age > stale_after:
            return "stale", f"no heartbeat for {_ago(heartbeat_age)}"
        if raw.status_hint == "busy":
            last_write = facts.last_line_at if facts else None
            silence = now - (last_write or raw.last_activity_at or now)
            if silence > stuck_after:
                return (
                    "attention",
                    f"likely waiting on input — quiet {_ago(silence)}",
                )
            return "busy", "working"
        return "idle", "waiting for you"

    # Desktop app: no PID, no heartbeat. Recency is all we have.
    age = now - (raw.last_activity_at or 0)
    if raw.last_activity_at is None:
        return "stale", "no activity timestamp"
    if age <= APP_BUSY_WINDOW:
        return "busy", "recently active"
    if age <= SCOPE_WINDOW:
        return "idle", f"last active {_ago(age)} ago"
    return "stale", f"last active {_ago(age)} ago"


def build_card(raw, facts, now):
    facts = facts or TranscriptFacts()
    status, reason = classify(raw, facts, now)

    tokens = {
        "input": facts.input_tokens,
        "output": facts.output_tokens,
        "cache_creation": facts.cache_creation_tokens,
        "cache_read": facts.cache_read_tokens,
    }
    model = raw.model or (facts.models_seen[-1] if facts.models_seen else None)

    running = [s for s in facts.subagents if s.returned_at is None]
    done = [s for s in facts.subagents if s.returned_at is not None]

    last_tool = None
    if facts.last_tool:
        last_tool = {
            "name": facts.last_tool.name,
            "detail": facts.last_tool.detail,
            "at": facts.last_tool.at,
        }

    return AgentCard(
        id=raw.id,
        source=raw.source,
        name=raw.name,
        cwd=raw.cwd,
        git_branch=facts.git_branch,
        model=model,
        status=status,
        status_reason=reason,
        last_tool=last_tool,
        subagents_running=len(running),
        subagents_done=len(done),
        subagents=[
            {
                "kind": s.kind,
                "description": s.description,
                "running": s.returned_at is None,
                "dispatched_at": s.dispatched_at,
            }
            # Outstanding dispatches first, then most recent completions.
            for s in running + list(reversed(done))[:10]
        ],
        tokens=tokens,
        cost_estimate=estimate_cost(model, tokens),
        started_at=raw.started_at,
        last_activity_at=raw.last_activity_at,
    )


def in_scope(card, now):
    if card.status != "stale":
        return True
    age = now - (card.last_activity_at or 0)
    return age <= SCOPE_WINDOW


def sort_cards(cards):
    return sorted(
        cards,
        key=lambda c: (STATUS_ORDER.get(c.status, 9), -(c.last_activity_at or 0)),
    )


class Collector:
    """Owns the transcript cache across polls. Build one, reuse it."""

    def __init__(self, home=None, app_support=None):
        self.home = home
        self.app_support = app_support
        self.cache = TranscriptCache()

    def snapshot(self, now=None):
        now = now or time.time()
        cards = []
        for raw in discover_all(self.home, self.app_support):
            try:
                facts = (
                    self.cache.facts_for(raw.transcript_path)
                    if raw.transcript_path
                    else None
                )
                card = build_card(raw, facts, now)
            except Exception as exc:               # one bad file must not blank the fleet
                card = _error_card(raw, exc, now)
            if in_scope(card, now):
                cards.append(card)

        cards = sort_cards(cards)
        counts = {"attention": 0, "busy": 0, "idle": 0, "stale": 0}
        for card in cards:
            counts[card.status] = counts.get(card.status, 0) + 1

        return {
            "generated_at": now,
            "counts": counts,
            "total": len(cards),
            "cards": [vars(c) for c in cards],
        }


def _error_card(raw, exc, now):
    return AgentCard(
        id=raw.id, source=raw.source, name=raw.name, cwd=raw.cwd,
        git_branch=None, model=raw.model, status="stale",
        status_reason="could not be read", last_tool=None,
        subagents_running=0, subagents_done=0, subagents=[],
        tokens={"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0},
        cost_estimate=None, started_at=raw.started_at,
        last_activity_at=raw.last_activity_at,
        error=f"{type(exc).__name__}: {exc}"[:200],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_model -v`
Expected: PASS, 15 tests

- [ ] **Step 5: Write the real-data smoke test**

`tests/test_smoke.py`:

```python
import time
import unittest
from fleet.model import Collector, STATUS_ORDER


class RealDataSmokeTest(unittest.TestCase):
    """Runs against whatever is actually on this machine. Must never raise."""

    def test_snapshot_is_sane(self):
        snap = Collector().snapshot()
        self.assertIn("cards", snap)
        self.assertLessEqual(abs(snap["generated_at"] - time.time()), 5)
        for card in snap["cards"]:
            self.assertIn(card["status"], STATUS_ORDER)
            self.assertTrue(card["name"])
            for key in ("input", "output", "cache_creation", "cache_read"):
                self.assertGreaterEqual(card["tokens"][key], 0)
            if card["cost_estimate"] is not None:
                self.assertGreaterEqual(card["cost_estimate"], 0)

    def test_second_snapshot_is_fast(self):
        collector = Collector()
        collector.snapshot()               # cold: full scan
        started = time.time()
        collector.snapshot()               # warm: incremental
        self.assertLess(time.time() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: Run the smoke test**

Run: `python3 -m unittest tests.test_smoke -v`
Expected: PASS. If the warm snapshot exceeds 1s, the incremental cache is re-reading whole files — debug Task 3 before continuing.

- [ ] **Step 7: Commit**

```bash
git add fleet/model.py tests/test_model.py tests/test_smoke.py
git commit -m "feat: status ladder, card assembly, and fleet collector"
```

---

### Task 7: HTTP server

**Files:**
- Create: `fleet/server.py`, `fleet/__main__.py`, `fleet/static/index.html` (placeholder shell — Task 8 replaces it)
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `Collector`
- Produces: `serve(port=8787, host="127.0.0.1")`; `make_handler(collector)`

Binds loopback only. Port conflict exits with a clear message rather than silently binding elsewhere.

- [ ] **Step 1: Write the failing test**

`tests/test_server.py`:

```python
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from fleet.model import Collector
from fleet.server import make_handler


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Collector()))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10)

    def test_api_returns_fleet_json(self):
        response = self.get("/api/fleet")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("application/json"))
        payload = json.loads(response.read())
        for key in ("generated_at", "counts", "total", "cards"):
            self.assertIn(key, payload)

    def test_cards_match_the_contract(self):
        payload = json.loads(self.get("/api/fleet").read())
        for card in payload["cards"]:
            for key in ("id", "source", "name", "status", "status_reason",
                        "tokens", "subagents_running", "subagents_done"):
                self.assertIn(key, card)

    def test_index_is_served(self):
        self.assertEqual(self.get("/").status, 200)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/../../../../etc/passwd")
        self.assertIn(ctx.exception.code, (403, 404))


if __name__ == "__main__":
    import urllib.error
    unittest.main()
```

Add `import urllib.error` to the top of the file alongside `urllib.request`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_server -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'fleet.server'`

- [ ] **Step 3: Write the implementation**

`fleet/server.py`:

```python
"""Loopback-only HTTP server for the fleet dashboard."""
import errno
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import Collector

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/fleet":
                return self._json(collector.snapshot())
            if path == "/":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            self._error(404, "not found")

        def _json(self, payload):
            body = json.dumps(payload, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, relative):
            target = os.path.normpath(os.path.join(STATIC_DIR, relative))
            # Refuse anything that escapes the static directory.
            if not target.startswith(STATIC_DIR + os.sep):
                return self._error(403, "forbidden")
            try:
                with open(target, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._error(404, "not found")
            ext = os.path.splitext(target)[1]
            self.send_response(200)
            self.send_header(
                "Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, message):
            body = message.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # quiet; launchd captures real errors via stderr

    return Handler


def serve(port=8787, host="127.0.0.1"):
    collector = Collector()

    # Warm the transcript cache off-thread so the first request is not slow.
    threading.Thread(target=collector.snapshot, daemon=True).start()

    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(collector))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            sys.stderr.write(
                f"Port {port} is already in use.\n"
                f"Either stop what is using it, or run with a different port:\n"
                f"    python3 -m fleet --port 8788\n"
            )
            raise SystemExit(1)
        raise

    sys.stderr.write(f"Agent Fleet on http://{host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
```

`fleet/__main__.py`:

```python
import argparse
from .server import serve


def main():
    parser = argparse.ArgumentParser(prog="fleet", description="Agent Fleet dashboard")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Loopback only by default. Do not expose this to a network — "
             "the page shows working directories and prompt fragments.",
    )
    args = parser.parse_args()
    serve(port=args.port, host=args.host)


if __name__ == "__main__":
    main()
```

`fleet/static/index.html` — minimal shell, replaced in Task 8:

```html
<!doctype html>
<meta charset="utf-8">
<title>Agent Fleet</title>
<pre id="out">loading…</pre>
<script>
async function tick() {
  const res = await fetch('/api/fleet');
  document.getElementById('out').textContent =
    JSON.stringify(await res.json(), null, 2);
}
tick(); setInterval(tick, 3000);
</script>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_server -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Run it for real**

Run: `python3 -m fleet`
Then open `http://127.0.0.1:8787` in a browser. Expected: raw JSON listing your live sessions, refreshing every 3s. Confirm your currently-running CLI sessions appear. Stop with Ctrl-C.

- [ ] **Step 6: Run the full suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS, everything from Tasks 1–7

- [ ] **Step 7: Commit**

```bash
git add fleet/server.py fleet/__main__.py fleet/static/index.html tests/test_server.py
git commit -m "feat: loopback HTTP server with fleet API"
```

---

### Task 8: The dashboard UI

**Files:**
- Rewrite: `fleet/static/index.html`
- Create: `fleet/static/app.css`, `fleet/static/app.js`, `fleet/static/icon.svg`

**Interfaces:**
- Consumes: `GET /api/fleet` — the payload shape produced by `Collector.snapshot()` in Task 6
- Produces: nothing consumed by later tasks

- [ ] **Step 1: Invoke the UI skill**

**REQUIRED:** Use the `ui-ux-pro-max` skill for this task. Do not hand-roll the visual design.

Give the skill the payload contract below and the requirements that follow.

```json
{
  "generated_at": 1786038213.5,
  "counts": {"attention": 1, "busy": 2, "idle": 3, "stale": 1},
  "total": 7,
  "cards": [{
    "id": "8c178277-...",
    "source": "cli",
    "name": "agent-action-a0",
    "cwd": "/Users/joekocovsky/Claude/Code/agent-action",
    "git_branch": "main",
    "model": "claude-opus-5",
    "status": "attention",
    "status_reason": "likely waiting on input — quiet 2m",
    "last_tool": {"name": "Bash", "detail": "Analyze transcripts", "at": 1786038100.0},
    "subagents_running": 1,
    "subagents_done": 2,
    "subagents": [
      {"kind": "Agent", "description": "Audit auth code",
       "running": true, "dispatched_at": 1786038000.0}
    ],
    "tokens": {"input": 672, "output": 362610,
               "cache_creation": 1132878, "cache_read": 60835864},
    "cost_estimate": 34.12,
    "started_at": 1786038157.9,
    "last_activity_at": 1786038213.5,
    "error": null
  }]
}
```

- [ ] **Step 2: Build the page to these requirements**

Non-negotiable, from the spec:

1. **`attention` cards sort first and are visually distinct.** They are the reason the dashboard exists.
2. **Hedged copy.** Render `status_reason` verbatim. Never add wording that asserts a session *is* blocked. It is a guess.
3. **Source is always visible.** A `cli` badge and an `app` badge, clearly different. The status dot means less on `app` cards — desktop sessions can never reach `attention` — so a viewer must always be able to tell which source they are looking at.
4. **Activity line:** `↳ {last_tool.name}: {last_tool.detail} · {age}`, where age is computed client-side from `last_tool.at` and re-rendered every second so it stays live between 3s polls. Omit the line when `last_tool` is null.
5. **Sub-agent rollup:** compact `↳ 1 running · 2 done`, expanding on click to the `subagents` list. Running items first. If `subagents_running` and `subagents_done` are both 0, show nothing.
6. **Cost:** tokens are primary, dollars secondary and explicitly marked an estimate. `cache_read` must be shown as its own figure, not folded into a single total — it dominates volume and is priced far lower. `cost_estimate: null` renders `—`, never `$0.00`.
7. **`error` non-null:** render an error badge on that card. The rest of the board must look normal.
8. **Empty state:** when `cards` is empty, say so plainly — "No agents active in the last 24 hours."
9. **Installed-app chrome:** `<title>Agent Fleet</title>`, a `<link rel="icon" href="/static/icon.svg">`, and a `<meta name="theme-color">`. These become the window title and Dock icon once installed as a Chrome app.
10. **Self-contained.** No CDN links, no external fonts. The server sends no network permissions and the page must work offline.
11. **Poll `/api/fleet` every 3s.** A failed fetch shows a subtle "reconnecting…" indicator and keeps the last good data on screen — it must not blank the board.

- [ ] **Step 3: Verify against live data**

Run: `python3 -m fleet`, open `http://127.0.0.1:8787`.

Check, with real sessions on screen:
- Your running CLI sessions appear with correct names and cwd.
- Desktop sessions appear with their human titles ("Morning brief").
- Ages tick up in real time between polls.
- Stopping a session (Ctrl-D in one terminal) moves that card to `stale` within ~10 minutes, and the card does not vanish.

- [ ] **Step 4: Verify the failure mode**

Stop the server with Ctrl-C while the page is open. Expected: the page shows "reconnecting…" and keeps displaying the last known cards. Restart the server; the page recovers on its own without a manual reload.

- [ ] **Step 5: Commit**

```bash
git add fleet/static
git commit -m "feat: fleet dashboard UI"
```

---

### Task 9: launchd install

**Files:**
- Create: `install.sh`, `uninstall.sh`, `README.md`

**Interfaces:**
- Consumes: `python3 -m fleet`
- Produces: nothing

Both scripts must be idempotent — safe to run twice.

- [ ] **Step 1: Write `install.sh`**

```bash
#!/bin/bash
# Install the Agent Fleet dashboard as a launchd user agent.
# Idempotent: safe to re-run to pick up changes.
set -euo pipefail

LABEL="com.joekocovsky.agentfleet"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(command -v python3)"
PORT="${PORT:-8787}"

if [ -z "$PYTHON" ]; then
  echo "python3 not found on PATH." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>fleet</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/agent-fleet.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/agent-fleet.log</string>
</dict>
</plist>
PLIST_EOF

# bootout first so a re-run picks up plist changes; ignore "not loaded".
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"

echo "Installed. Dashboard: http://127.0.0.1:$PORT"
echo "Logs: ~/Library/Logs/agent-fleet.log"
echo
echo "To make it feel like a native app:"
echo "  1. Open http://127.0.0.1:$PORT in Chrome"
echo "  2. ⋮ menu → Cast, Save & Share → Install page as app"
echo "  3. It gets its own Dock icon and window. ⌘-Tab to it like any app."
```

- [ ] **Step 2: Write `uninstall.sh`**

```bash
#!/bin/bash
# Remove the Agent Fleet launchd agent. Leaves the repo untouched.
set -euo pipefail

LABEL="com.joekocovsky.agentfleet"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

echo "Uninstalled. If you installed the Chrome app window, remove it from"
echo "chrome://apps as well."
```

- [ ] **Step 3: Make them executable and install**

```bash
chmod +x install.sh uninstall.sh
./install.sh
```

Expected: the install prints the URL and the Chrome instructions.

- [ ] **Step 4: Verify it is actually running**

```bash
launchctl print "gui/$UID/com.joekocovsky.agentfleet" | grep -E "state|pid"
curl -s http://127.0.0.1:8787/api/fleet | head -c 200
```

Expected: state `running` with a pid, and JSON from curl.

- [ ] **Step 5: Verify it survives a crash**

```bash
kill "$(launchctl print "gui/$UID/com.joekocovsky.agentfleet" | awk '/pid = / {print $3}')"
sleep 3
curl -s http://127.0.0.1:8787/api/fleet | head -c 60
```

Expected: `KeepAlive` restarted it; curl still returns JSON.

- [ ] **Step 6: Confirm it is not reachable off-machine**

```bash
IP=$(ipconfig getifaddr en0 2>/dev/null || echo "")
[ -n "$IP" ] && curl -s --max-time 3 "http://$IP:8787/api/fleet" && echo "EXPOSED — FIX THIS" || echo "loopback only, correct"
```

Expected: `loopback only, correct`. If it prints `EXPOSED`, the server is not binding `127.0.0.1` — stop and fix before finishing.

- [ ] **Step 7: Write `README.md`**

Cover: what it does, the two data sources it reads, that it is read-only, `./install.sh` / `./uninstall.sh`, the Chrome app-window steps, how to edit `fleet/prices.py`, the sub-agent limitation (dispatch-and-return only, no view inside a sub-agent), and that desktop sessions cannot report `attention`.

- [ ] **Step 8: Commit**

```bash
git add install.sh uninstall.sh README.md
git commit -m "feat: launchd install and README"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| Data sources A and B | 4 |
| Transcript schema, tokens, tool calls | 2 |
| Sub-agent constraint (dispatch-and-return) | 2, 6, 8 |
| Unified `AgentCard` + name resolution | 1, 6 |
| Status ladder incl. desktop mapping | 6 |
| Fleet scope (24h) | 6 |
| Incremental performance | 3 |
| Cost + cache_read separation | 5, 8 |
| Error handling | 4, 6, 7, 8 |
| Testing | every task + `test_smoke` |
| Runtime / launchd / app window | 9 |
| UI requirements | 8 |

No gaps.

**Type consistency:** `TranscriptFacts` field names (`cache_creation_tokens`, `cache_read_tokens`) differ from the `AgentCard.tokens` dict keys (`cache_creation`, `cache_read`). This is deliberate — the dict is the JSON wire format — and the mapping happens in exactly one place, `build_card`. `estimate_cost` consumes the dict form, matching. `parse_lines(lines, facts)` returns the same object it mutates, which Task 3 relies on.

**Known rough edges, accepted for MVP:**
- Desktop `busy` is a 60-second recency guess; a desktop session thinking for 90 seconds reads as `idle`.
- `attention` can fire on a genuinely long-running tool call (a 5-minute build). The reason string stays hedged for exactly this reason.
- `PRICES` values need verification by the user before any dollar figure is trusted.
