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

    def test_warm_snapshot_is_far_cheaper_than_cold(self):
        """The incremental cache must be measurably doing its job.

        An absolute bound (the old '< 1.0s') cannot discriminate: the cold
        full-corpus scan measures ~0.46s on this machine, comfortably inside
        it, so ripping out the cache entirely left the assertion green. The
        honest measurement is the ratio between a cold scan and a warm one.

        Robustness: the warm figure is the best of three polls, so a GC pause
        or a busy machine cannot fake a slow warm poll — but no number of
        retries can make a full re-scan cheap, which is what the ratio
        detects. The test skips rather than flakes if this machine has too
        little transcript data for a cold scan to be measurable at all.
        """
        collector = Collector()
        started = time.time()
        collector.snapshot()                       # cold: full scan
        cold = time.time() - started

        if cold < 0.05:
            self.skipTest(f"corpus too small to measure (cold scan {cold:.4f}s)")

        warm = min(self._timed(collector) for _ in range(3))
        self.assertLess(
            warm, cold / 5.0,
            f"warm poll {warm:.4f}s vs cold {cold:.4f}s — the incremental "
            f"transcript cache is not saving anything",
        )

    @staticmethod
    def _timed(collector):
        started = time.time()
        collector.snapshot()
        return time.time() - started


if __name__ == "__main__":
    unittest.main()
