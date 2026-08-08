"""Parse Codex CLI rollout transcripts into the same TranscriptFacts.

Codex writes `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`. Same
JSONL container as Claude, entirely different shape: every line is
`{timestamp, type, payload}` and the interesting parts live in the payload.

Three differences from `transcript.parse_lines` that matter:

1. **Token usage is CUMULATIVE.** `event_msg/token_count` carries a running
   `info.total_token_usage` for the whole session, so the correct read is
   last-wins, not a sum. Adding them up would multiply the real figure by the
   number of turns. Last-wins also survives the incremental cache unchanged:
   a later line simply overwrites.

2. **`input_tokens` INCLUDES the cached portion.** Anthropic reports cache
   reads separately from input; OpenAI nests them. The cached subset is
   subtracted here so both providers reach `estimate_cost` meaning the same
   thing by `input`.

3. **Sub-agents do not exist** in this format, so `subagents` stays empty and
   a Codex card simply shows no rollup.
"""
import json
import os

from .types import ToolCall, TranscriptFacts
from .timeutil import iso_to_epoch

_MAX_DETAIL = 90
TOOL_PAYLOADS = ("function_call", "local_shell_call", "custom_tool_call")


def _dig(obj, *keys):
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _tool_detail(payload):
    """A short hint about what the call is doing."""
    for key in ("name", "command", "action"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.replace("\n", " ").strip()[:_MAX_DETAIL]
    args = payload.get("arguments") or payload.get("input")
    if isinstance(args, str) and args.strip():
        return args.replace("\n", " ").strip()[:_MAX_DETAIL]
    return ""


def parse_lines(lines, facts):
    """Fold Codex rollout lines into `facts`, mutating and returning it.

    Same contract as `transcript.parse_lines` so `TranscriptCache` can drive
    either parser over appended bytes.
    """
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
        if at is not None and (facts.last_line_at is None or at > facts.last_line_at):
            facts.last_line_at = at

        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue

        # Model appears in a few places depending on the line; last real one wins.
        model = (
            _dig(payload, "state", "collaboration_mode", "model")
            or payload.get("model")
        )
        if isinstance(model, str) and model and model not in facts.models_seen:
            facts.models_seen.append(model)

        if kind == "session_meta":
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                facts.git_branch = facts.git_branch or None   # not recorded by Codex

        if kind == "event_msg" and payload.get("type") == "token_count":
            usage = _dig(payload, "info", "total_token_usage")
            if isinstance(usage, dict):
                # Cumulative: assign, never accumulate.
                cached = _int(usage.get("cached_input_tokens"))
                total_in = _int(usage.get("input_tokens"))
                facts.input_tokens = max(0, total_in - cached)
                facts.cache_read_tokens = cached
                facts.cache_creation_tokens = _int(usage.get("cache_write_input_tokens"))
                facts.cache_creation_1h_tokens = 0        # no TTL tiers here
                facts.output_tokens = _int(usage.get("output_tokens"))
                facts.reasoning_output_tokens = _int(
                    usage.get("reasoning_output_tokens")
                )

        if kind == "response_item" and payload.get("type") in TOOL_PAYLOADS:
            name = payload.get("name") or payload.get("type")
            facts.last_tool = ToolCall(
                name=str(name), detail=_tool_detail(payload), at=at
            )

    return facts


def session_meta(path):
    """Read the header line for cwd and session id without parsing the body.

    Rollouts run to megabytes; the metadata is on the first line.
    """
    try:
        with open(path, "r", errors="replace") as fh:
            for _ in range(20):                 # header is line 1, allow slack
                line = fh.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if record.get("type") == "session_meta":
                    payload = record.get("payload") or {}
                    return {
                        "session_id": payload.get("session_id") or payload.get("id"),
                        "cwd": payload.get("cwd") or "",
                        "started_at": iso_to_epoch(payload.get("timestamp")),
                        "cli_version": payload.get("cli_version"),
                    }
    except OSError:
        return None
    return None


def _int(value):
    return value if isinstance(value, int) else 0
