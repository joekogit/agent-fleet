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
