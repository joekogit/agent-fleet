"""Normalize sessions from any source into one AgentCard, and rank them."""
import os
import threading
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
        # snapshot() performs multi-step read-modify-write on self.cache's
        # shared mutable state (per-transcript offset/mtime/size). The server
        # calls snapshot() from a background warm thread and from a new
        # thread per HTTP connection, so this must serialize those calls.
        self._lock = threading.Lock()

    def snapshot(self, now=None):
        with self._lock:
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
