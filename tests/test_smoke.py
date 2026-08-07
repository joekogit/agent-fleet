import time
import unittest
from fleet.model import Collector, STATUS_ORDER


class RealDataSmokeTest(unittest.TestCase):
    """Runs against whatever is actually on this machine. Must never raise."""

    def test_snapshot_is_sane(self):
        snap = Collector().snapshot()
        self.assertIn("cards", snap)
        self.assertLessEqual(abs(snap["generated_at"] - time.time()), 5)
        for card in snap["cards"]:
            self.assertIn(card["status"], STATUS_ORDER)
            self.assertTrue(card["name"])
            for key in ("input", "output", "cache_creation", "cache_read"):
                self.assertGreaterEqual(card["tokens"][key], 0)
            if card["cost_estimate"] is not None:
                self.assertGreaterEqual(card["cost_estimate"], 0)

    def test_second_snapshot_is_fast(self):
        collector = Collector()
        collector.snapshot()               # cold: full scan
        started = time.time()
        collector.snapshot()               # warm: incremental
        self.assertLess(time.time() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
