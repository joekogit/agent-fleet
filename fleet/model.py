"""Normalize sessions from any source into one AgentCard, and rank them."""
import os
import subprocess
import threading
import time

from .types import AgentCard, TranscriptFacts
from .transcript import TranscriptCache
from .sources import discover_all
from .prices import estimate_cost

STUCK_AFTER = 60.0        # busy + transcript silence past this => likely blocked
SCOPE_WINDOW = 86400.0    # show live sessions plus anything active in 24h
APP_BUSY_WINDOW = 60.0    # desktop has no heartbeat; recent activity == busy

# A live process whose start time is later than the session's own startedAt by
# more than this cannot be the process that wrote the session file: the PID was
# recycled. Measured on this machine, a genuine process starts 1-5 SECONDS
# BEFORE its session file's startedAt, so the real signal is nowhere near the
# margin.
PID_REUSE_SLACK = 120.0
_PROC_START_TTL = 30.0    # re-ask `ps` about a PID at most this often

# Sentinel Claude writes as `message.model` for locally-generated turns.
SYNTHETIC_MODEL = "<synthetic>"

STATUS_ORDER = {"attention": 0, "busy": 1, "idle": 2, "stale": 3}

MAX_DONE_SHOWN = 10       # completed dispatches listed per card; the rest are counted

_proc_start_cache = {}    # pid -> (checked_at, start_epoch|None)
_proc_start_lock = threading.Lock()


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


def _parse_etime(text):
    """`ps -o etime=` -> elapsed seconds. Format: [[dd-]hh:]mm:ss."""
    text = (text or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        head, text = text.split("-", 1)
        days = int(head)
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _proc_elapsed(pid):
    """(elapsed seconds, monotonic clock when measured), or None.

    Cached briefly: polling is every 3s and spawning `ps` per session per
    poll is waste. Freshness is tracked on the monotonic clock, never on a
    caller-supplied `now`, so a caller reasoning about a synthetic clock
    cannot poison the cache for everyone else. A PID recycled inside the TTL
    is simply noticed one TTL late.
    """
    stamp = time.monotonic()
    with _proc_start_lock:
        cached = _proc_start_cache.get(pid)
        if cached and stamp - cached[1] < _PROC_START_TTL:
            return cached
    elapsed = None
    try:
        out = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
        elapsed = _parse_etime(out.stdout.decode("ascii", "replace"))
    except (OSError, ValueError, subprocess.SubprocessError):
        elapsed = None
    record = (elapsed, time.monotonic())
    with _proc_start_lock:
        _proc_start_cache[pid] = record
    return record


def prewarm_proc_starts(pids):
    """Resolve every PID's elapsed time in ONE `ps`, before the lock is taken.

    Called per poll from `Collector.snapshot`. Without it, `_proc_elapsed`
    spawns one `ps` per live CLI session from inside `Collector._lock`, so
    every HTTP request serializes behind N subprocess spawns. One batched
    call outside the lock costs a single exec and leaves the locked region
    doing nothing but cache lookups.

    A PID `ps` does not report is cached as unknown, which fails open in
    `_pid_was_reused` — same as any other unreadable case.
    """
    stamp = time.monotonic()
    with _proc_start_lock:
        wanted = [
            pid for pid in set(pids)
            if pid and not (
                _proc_start_cache.get(pid)
                and stamp - _proc_start_cache[pid][1] < _PROC_START_TTL
            )
        ]
    if not wanted:
        return                                  # every entry still fresh

    parsed = {}
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,etime=", "-p", ",".join(str(p) for p in wanted)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        )
        for line in out.stdout.decode("ascii", "replace").splitlines():
            fields = line.split()
            if len(fields) != 2:
                continue
            try:
                parsed[int(fields[0])] = _parse_etime(fields[1])
            except ValueError:
                continue
    except (OSError, subprocess.SubprocessError):
        parsed = {}

    measured_at = time.monotonic()
    with _proc_start_lock:
        for pid in wanted:
            _proc_start_cache[pid] = (parsed.get(pid), measured_at)


def forget_proc_starts():
    """Drop the PID start-time cache. For tests that need a clean slate."""
    with _proc_start_lock:
        _proc_start_cache.clear()


def proc_start_epoch(pid, now):
    """When the process holding `pid` started, or None if we cannot tell.

    `ps -o etime=` is read rather than `lstart=` because elapsed time is
    locale-independent; `lstart` is rendered in the caller's locale and in
    local time, while the session file's own `procStart` string is UTC.
    """
    elapsed, measured_at = _proc_elapsed(pid)
    if elapsed is None:
        return None
    return now - (elapsed + max(0.0, time.monotonic() - measured_at))


def _pid_was_reused(raw, now):
    """True only when the live PID provably belongs to a DIFFERENT process.

    `os.kill(pid, 0)` proves *some* process holds the PID, not that it is the
    one that wrote this session file. The OS recycles PIDs, so a long-dead
    session could otherwise be resurrected as `busy`. A recycled PID's process
    necessarily started after the old one died, and therefore well after the
    session's own startedAt.

    Fails open: if `ps` is unavailable or the session has no startedAt we
    cannot prove reuse, and liveness wins. Wrongly hiding a live session is
    the failure this dashboard exists to prevent.
    """
    if raw.started_at is None:
        return False
    start = proc_start_epoch(raw.pid, now)
    if start is None:
        return False
    return start > raw.started_at + PID_REUSE_SLACK


def _ago(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def classify(raw, facts, now, stuck_after=STUCK_AFTER):
    """Return (status, human reason).

    The 'attention' state is INFERRED, never observed. Its reason string must
    stay hedged — the dashboard cannot actually see a permission prompt.

    A live PID is the ONLY liveness signal for a CLI session. `updatedAt` is
    an activity timestamp, not a heartbeat: it stops advancing the moment a
    session goes quiet, so a session idle for 13 hours is still perfectly
    alive. Age of `updatedAt` therefore never produces `stale` — the 24h
    `in_scope` window handles genuinely old sessions.
    """
    if raw.source == "cli":
        if not _pid_alive(raw.pid):
            return "stale", "process is gone"
        if _pid_was_reused(raw, now):
            return "stale", "process is gone — PID now belongs to another process"
        if raw.status_hint == "busy":
            last_write = facts.last_line_at if facts else None
            silence = now - (last_write or raw.last_activity_at or now)
            if silence > stuck_after:
                outstanding = _running_subagents(facts)
                if outstanding:
                    # A parent goes silent while a sub-agent runs: sub-agent
                    # turns are never written to the parent's transcript. It
                    # is working, not waiting on the user.
                    count = len(outstanding)
                    plural = "" if count == 1 else "s"
                    return (
                        "busy",
                        f"working — {count} sub-agent{plural} running "
                        f"({_ago(silence)} since its own last write)",
                    )
                return (
                    "attention",
                    f"likely waiting on input — quiet {_ago(silence)}",
                )
            return "busy", "working"
        return "idle", "finished — ready when you are"

    # Desktop app: no PID, no heartbeat. Recency is all we have.
    age = now - (raw.last_activity_at or 0)
    if raw.last_activity_at is None:
        return "stale", "no activity timestamp"
    if age <= APP_BUSY_WINDOW:
        return "busy", "recently active"
    if age <= SCOPE_WINDOW:
        return "idle", f"last active {_ago(age)} ago"
    return "stale", f"last active {_ago(age)} ago"


def _running_subagents(facts):
    if not facts:
        return []
    return [s for s in facts.subagents if s.returned_at is None]


def display_model(raw, facts):
    """The last REAL model seen.

    '<synthetic>' is the sentinel Claude writes for locally-generated
    assistant turns (interrupts, API errors). It is not a model: showing it
    hides the true model and, because it has no price, suppresses the cost
    estimate for the whole session.
    """
    if raw.model:
        return raw.model
    real = [m for m in facts.models_seen if m != SYNTHETIC_MODEL]
    return real[-1] if real else None


def build_card(raw, facts, now):
    facts = facts or TranscriptFacts()
    status, reason = classify(raw, facts, now)

    tokens = {
        "input": facts.input_tokens,
        "output": facts.output_tokens,
        "cache_creation": facts.cache_creation_tokens,
        # Portion of the above written with the 1-hour TTL, priced at 2x base
        # input instead of 1.25x. The UI shows the total; only estimate_cost
        # needs the split.
        "cache_creation_1h": facts.cache_creation_1h_tokens,
        "cache_read": facts.cache_read_tokens,
    }
    model = display_model(raw, facts)

    running = _running_subagents(facts)
    done = [s for s in facts.subagents if s.returned_at is not None]
    shown_done = list(reversed(done))[:MAX_DONE_SHOWN]

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
            for s in running + shown_done
        ],
        tokens=tokens,
        cost_estimate=estimate_cost(model, tokens),
        started_at=raw.started_at,
        last_activity_at=raw.last_activity_at,
        subagents_omitted=len(done) - len(shown_done),
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
        # snapshot() performs multi-step read-modify-write on self.cache's
        # shared mutable state (per-transcript offset/mtime/size). The server
        # calls snapshot() from a background warm thread and from a new
        # thread per HTTP connection, so this must serialize those calls.
        self._lock = threading.Lock()

    def snapshot(self, now=None):
        now = now or time.time()

        # Both of these run OUTSIDE the lock. `discover_all` only reads the
        # filesystem into fresh objects, and `prewarm_proc_starts` only fills
        # its own separately-locked cache. The collector lock exists to guard
        # the incremental TranscriptCache; holding it across a directory walk
        # and N subprocess spawns would serialize every request behind them.
        sessions = discover_all(self.home, self.app_support)
        prewarm_proc_starts(
            [s.pid for s in sessions if s.source == "cli" and s.pid]
        )

        with self._lock:
            cards = []
            for raw in sessions:
                try:
                    facts = (
                        self.cache.facts_for(raw.transcript_path)
                        if raw.transcript_path
                        else None
                    )
                    card = build_card(raw, facts, now)
                except Exception as exc:            # one bad file must not blank the fleet
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
