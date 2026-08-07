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
    # Completed dispatches beyond the display cap. The rollup header reports
    # the true total, so the list must say what it is not showing.
    subagents_omitted: int = 0
