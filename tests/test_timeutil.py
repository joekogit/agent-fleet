import unittest
from datetime import datetime, timezone
from fleet.timeutil import iso_to_epoch, ms_to_epoch


class TestIsoToEpoch(unittest.TestCase):
    def test_parses_transcript_format(self):
        # Exact format seen in real transcripts: '2026-08-06T17:22:10.050Z'
        expected = datetime(
            2026, 8, 6, 17, 22, 10, 50000, tzinfo=timezone.utc
        ).timestamp()
        self.assertAlmostEqual(
            iso_to_epoch("2026-08-06T17:22:10.050Z"), expected, places=3
        )

    def test_returns_none_for_garbage(self):
        self.assertIsNone(iso_to_epoch("not a date"))
        self.assertIsNone(iso_to_epoch(None))
        self.assertIsNone(iso_to_epoch(""))


class TestMsToEpoch(unittest.TestCase):
    def test_converts_milliseconds(self):
        self.assertEqual(ms_to_epoch(1786038157970), 1786038157.970)

    def test_returns_none_for_none(self):
        self.assertIsNone(ms_to_epoch(None))


if __name__ == "__main__":
    unittest.main()
