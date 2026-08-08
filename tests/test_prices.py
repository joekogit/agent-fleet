import unittest
from unittest.mock import patch
from fleet.prices import estimate_cost, normalize_model, PRICES, ModelPrice

TOKENS = {
    "input": 1_000_000,
    "output": 1_000_000,
    "cache_creation": 0,
    "cache_creation_1h": 0,
    "cache_read": 0,
}


def price(**kwargs):
    base = dict(input=0.0, output=0.0, cache_write_5m=0.0,
                cache_write_1h=0.0, cache_read=0.0)
    base.update(kwargs)
    return ModelPrice(**base)


class EstimateCostTest(unittest.TestCase):
    def test_known_model_sums_input_and_output(self):
        with patch.dict(PRICES, {"test-model": price(input=10.0, output=20.0)}):
            self.assertAlmostEqual(estimate_cost("test-model", TOKENS), 30.0)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(estimate_cost("no-such-model", TOKENS))

    def test_none_model_returns_none(self):
        self.assertIsNone(estimate_cost(None, TOKENS))

    def test_cache_read_priced_separately_from_input(self):
        tokens = dict(TOKENS, input=0, output=0, cache_read=1_000_000)
        with patch.dict(PRICES, {"cheap": price(input=10.0, cache_read=1.0)}):
            # Priced as cache_read (1.0), NOT as input (10.0).
            self.assertAlmostEqual(estimate_cost("cheap", tokens), 1.0)

    def test_every_shipped_price_is_complete(self):
        for name, entry in PRICES.items():
            for field in ("input", "output", "cache_write_5m",
                          "cache_write_1h", "cache_read"):
                self.assertIsInstance(getattr(entry, field), float, name)


class CacheWriteTierTest(unittest.TestCase):
    """The two cache-write tiers must not be collapsed into one rate.

    A 5-minute write costs 1.25x base input; an hour costs 2x. Claude Code
    uses the 1-hour TTL, so pricing everything at the 5-minute rate
    understates cache creation by 60%.
    """

    TIERED = {"tiered": price(cache_write_5m=1.0, cache_write_1h=8.0)}

    def _cost(self, total, one_hour):
        tokens = dict(TOKENS, input=0, output=0,
                      cache_creation=total, cache_creation_1h=one_hour)
        with patch.dict(PRICES, self.TIERED):
            return estimate_cost("tiered", tokens)

    def test_all_five_minute(self):
        self.assertAlmostEqual(self._cost(1_000_000, 0), 1.0)

    def test_all_one_hour(self):
        self.assertAlmostEqual(self._cost(1_000_000, 1_000_000), 8.0)

    def test_mixed_splits_by_the_recorded_portion(self):
        # 250k at 1h (8.0) + 750k at 5m (1.0) = 2.0 + 0.75
        self.assertAlmostEqual(self._cost(1_000_000, 250_000), 2.75)

    def test_missing_breakdown_falls_back_to_five_minute(self):
        tokens = dict(TOKENS, input=0, output=0, cache_creation=1_000_000)
        tokens.pop("cache_creation_1h")
        with patch.dict(PRICES, self.TIERED):
            self.assertAlmostEqual(estimate_cost("tiered", tokens), 1.0)

    def test_one_hour_portion_cannot_exceed_the_total(self):
        """Guards against a malformed transcript inflating the estimate."""
        self.assertAlmostEqual(self._cost(1_000_000, 5_000_000), 8.0)


class NormalizeModelTest(unittest.TestCase):
    def test_dated_snapshot_resolves_to_the_base_model(self):
        self.assertEqual(
            normalize_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5"
        )

    def test_dated_snapshot_is_priced(self):
        """Without normalization a dated id renders no cost at all."""
        self.assertIsNotNone(estimate_cost("claude-haiku-4-5-20251001", TOKENS))

    def test_undated_model_is_unchanged(self):
        self.assertEqual(normalize_model("claude-opus-5"), "claude-opus-5")

    def test_none_stays_none(self):
        self.assertIsNone(normalize_model(None))


class ShippedRatesTest(unittest.TestCase):
    """Spot-check the published rates this table was transcribed from.

    Sourced from platform.claude.com/docs/en/about-claude/pricing on
    2026-08-07. A previous version of this table used the retired Opus 4.1
    rates ($15/$75) for every Opus model, overstating cost by ~3x.
    """

    def test_opus_5_is_not_on_retired_opus_4_1_rates(self):
        self.assertEqual(PRICES["claude-opus-5"].input, 5.0)
        self.assertEqual(PRICES["claude-opus-5"].output, 25.0)
        self.assertEqual(PRICES["claude-opus-4-1"].input, 15.0)

    def test_cache_tiers_are_the_published_multiples_of_base_input(self):
        """Every entry is Anthropic, whose cache prices are fixed multiples.

        This table is Claude-only by design. A provider with different cache
        economics cannot simply be added here — it would need its own pricing
        shape, and this assertion would stop being true.
        """
        for name, entry in PRICES.items():
            with self.subTest(model=name):
                self.assertAlmostEqual(entry.cache_write_5m, entry.input * 1.25)
                self.assertAlmostEqual(entry.cache_write_1h, entry.input * 2.0)
                self.assertAlmostEqual(entry.cache_read, entry.input * 0.1)

    def test_table_is_claude_only(self):
        """Guards the walked-back scope: no non-Anthropic model ids."""
        for name in PRICES:
            self.assertTrue(name.startswith("claude-"), name)

    def test_models_seen_on_this_machine_are_all_priced(self):
        for model in ("claude-opus-5", "claude-fable-5", "claude-sonnet-5",
                      "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6",
                      "claude-haiku-4-5"):
            with self.subTest(model=model):
                self.assertIn(model, PRICES)


if __name__ == "__main__":
    unittest.main()
