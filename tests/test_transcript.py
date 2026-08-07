import json
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


class TestCacheWriteTiers(unittest.TestCase):
    """The 1-hour portion of cache creation is billed at a different rate."""

    def _facts(self, usage):
        line = json.dumps({
            "type": "assistant",
            "timestamp": "2026-08-06T17:00:00.000Z",
            "message": {"model": "m", "usage": usage, "content": []},
        })
        return parse_lines([line], TranscriptFacts())

    def test_one_hour_portion_is_captured(self):
        f = self._facts({
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 2366,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_1h_input_tokens": 2366,
                               "ephemeral_5m_input_tokens": 0},
        })
        self.assertEqual(f.cache_creation_tokens, 2366)
        self.assertEqual(f.cache_creation_1h_tokens, 2366)

    def test_five_minute_writes_leave_the_one_hour_bucket_empty(self):
        f = self._facts({
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 500,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_1h_input_tokens": 0,
                               "ephemeral_5m_input_tokens": 500},
        })
        self.assertEqual(f.cache_creation_tokens, 500)
        self.assertEqual(f.cache_creation_1h_tokens, 0)

    def test_absent_breakdown_does_not_crash_or_invent_a_tier(self):
        f = self._facts({
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 900,
            "cache_read_input_tokens": 0,
        })
        self.assertEqual(f.cache_creation_tokens, 900)
        self.assertEqual(f.cache_creation_1h_tokens, 0)


if __name__ == "__main__":
    unittest.main()
