import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
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

    def test_nanosecond_mtime_detects_same_second_append(self):
        """Verify that nanosecond mtime (not float-second) detects writes in the same second.

        This test forces the exact scenario the fix addresses: two writes to the same file
        that have the same float-second mtime (same-second collision) but different
        nanosecond mtime values. Without the nanosecond comparison, the cache would
        incorrectly see the file as unchanged and skip reading the appended data.

        The test mocks os.stat to return identical float mtime and size but different
        mtime_ns values. This proves that float-second comparison would fail to detect
        the change, while nanosecond comparison correctly detects it.
        """
        self.write(LINE_A)
        cache = TranscriptCache()

        # First poll: read the file normally
        facts_1 = cache.facts_for(self.path)
        self.assertEqual(facts_1.input_tokens, 10)

        # Verify _Entry stores mtime_ns (not mtime)
        cached_entry = cache._entries[self.path]
        self.assertTrue(hasattr(cached_entry, "mtime_ns"), "_Entry should have mtime_ns attribute")
        self.assertFalse(hasattr(cached_entry, "mtime"), "_Entry should not have mtime attribute")
        self.assertIsNotNone(cached_entry.mtime_ns, "mtime_ns should be set after first read")

        # Capture the real stat info from the first call
        real_stat = os.stat(self.path)
        original_size = real_stat.st_size
        original_mtime_ns = real_stat.st_mtime_ns
        real_mtime_float = real_stat.st_mtime

        # Now append LINE_B to the file (actual file change)
        self.write(LINE_B, mode="a")

        # Create a mock stat that simulates a same-second write collision:
        # - Same float mtime (collision at float precision — same second)
        # - Same size (we'll mock the read to handle actual appended data)
        # - Different mtime_ns (different at nanosecond precision — the fix detects this)
        new_mtime_ns = original_mtime_ns + 500000  # Different nanosecond value (+ 0.5 ms)

        def mock_stat(path):
            """Return stat with same float mtime and size, but different mtime_ns.

            This simulates two writes in the same second:
            - At float precision: identical (1234567890.0 == 1234567890.0) → collision
            - At nanosecond precision: different → the fix correctly detects this
            """
            mock_result = MagicMock()
            mock_result.st_ino = real_stat.st_ino
            mock_result.st_mtime = real_mtime_float       # SAME at float precision (same second)
            mock_result.st_mtime_ns = new_mtime_ns        # DIFFERENT at nanosecond precision
            mock_result.st_size = original_size           # Report original size (stat doesn't reflect new data)
            return mock_result

        # Patch os.stat for the cache (but file remains actually changed on disk)
        with patch("os.stat", side_effect=mock_stat):
            facts_2 = cache.facts_for(self.path)

        # With nanosecond comparison, the cache detects the mtime_ns change and reads the appended data.
        # The facts object is mutated in-place by parse_lines, so facts_1 and facts_2 reference
        # the same object, but it should be updated with the appended data.
        self.assertEqual(facts_2.input_tokens, 15,
                         "Nanosecond mtime_ns comparison must detect same-second appends")


if __name__ == "__main__":
    unittest.main()
