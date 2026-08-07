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
