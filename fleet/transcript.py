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


class _Entry:
    __slots__ = ("inode", "mtime_ns", "size", "offset", "facts")

    def __init__(self, inode):
        self.inode = inode
        self.mtime_ns = None
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
        elif stat.st_size == entry.size and stat.st_mtime_ns == entry.mtime_ns:
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
            entry.mtime_ns = stat.st_mtime_ns
            entry.size = stat.st_size
            return entry.facts

        complete = chunk[: consumed + 1]
        entry.offset += len(complete)
        entry.mtime_ns = stat.st_mtime_ns
        entry.size = stat.st_size

        text = complete.decode("utf-8", errors="replace")
        parse_lines(text.splitlines(), entry.facts)
        return entry.facts
