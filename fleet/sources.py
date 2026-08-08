"""Discover agent sessions from the CLI and the desktop app.

Each adapter answers one question: what sessions exist, and where is each
one's transcript? Neither reads transcript contents.
"""
import glob
import json
import os
import time
import sys
import traceback

from .types import RawSession
from .timeutil import ms_to_epoch

DEFAULT_HOME = os.path.expanduser("~")
DEFAULT_APP_SUPPORT = os.path.expanduser(
    "~/Library/Application Support/Claude"
)
# Exited CLI sessions stay on the board this long, matching the spec's 24h
# fleet scope. Beyond it they are not even constructed.
ORPHAN_WINDOW = 86400.0

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
    """~/.claude/sessions/<PID>.json plus ~/.claude/projects/**/<sessionId>.jsonl

    Two discovery passes, because the registry only describes LIVE processes.
    `~/.claude/sessions/<PID>.json` is deleted when a session exits, but its
    transcript remains — so a registry-only scan can never satisfy the spec's
    "alive now, plus anything active in the last 24 hours". A session that ran
    an hour ago and quit was not being aged into `stale`; it was never found.
    The second pass sweeps recent transcripts and emits the ones the registry
    does not already account for.
    """

    source = "cli"

    def __init__(self, home=None):
        self.home = home or DEFAULT_HOME

    def discover(self):
        live = self._from_registry()
        seen = set(s.id for s in live)
        return live + self._orphan_transcripts(seen)

    def _from_registry(self):
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

    def _orphan_transcripts(self, seen):
        """Recent transcripts with no live registry entry — i.e. exited sessions.

        Bounded to ORPHAN_WINDOW so a machine with months of history does not
        build hundreds of cards the scope filter would only discard again.
        `pid=None` is deliberate: the status ladder reads that as "cannot prove
        liveness" and renders `stale`, which is exactly what these are.
        """
        projects_dir = os.path.join(self.home, ".claude", "projects")
        cutoff = time.time() - ORPHAN_WINDOW
        out = []
        for path in glob.glob(os.path.join(projects_dir, "*", "*.jsonl")):
            session_id = os.path.basename(path)[: -len(".jsonl")]
            if session_id in seen:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            cwd = _transcript_cwd(path)
            out.append(
                RawSession(
                    id=session_id,
                    source=self.source,
                    name=_orphan_name(cwd, session_id),
                    cwd=cwd,
                    transcript_path=path,
                    status_hint=None,       # no registry entry to hint with
                    pid=None,               # exited: liveness cannot be proven
                    model=None,             # the transcript supplies it
                    effort=None,
                    started_at=None,
                    last_activity_at=mtime,
                )
            )
        return out


def _orphan_name(cwd, session_id):
    """`code-a1f2` — directory plus a stable suffix from the session id.

    Several sessions commonly run in the same directory (often `$HOME`), and
    without a suffix they render as a row of identical cards. This is NOT the
    name Claude derived for the live session — that lived in the registry file
    which is deleted on exit — so it only has to be stable and distinguishable.
    """
    base = os.path.basename(cwd)
    if not base:
        return session_id[:8]
    return "%s-%s" % (base, session_id[:4])


def _transcript_cwd(path, max_lines=30):
    """Recover the working directory from the head of a transcript.

    The directory name is a slugged path (`-Users-me-Code-thing`) that cannot
    be reversed unambiguously — a dash in a real directory name is
    indistinguishable from a separator — so the value recorded inside the file
    is read instead.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(record, dict) and record.get("cwd"):
                    return record["cwd"]
    except OSError:
        pass
    return ""


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
            traceback.print_exc(file=sys.stderr)
            continue
    return sessions
