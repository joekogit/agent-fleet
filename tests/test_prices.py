import unittest
from unittest.mock import patch
from fleet.prices import estimate_cost, PRICES, ModelPrice

TOKENS = {
    "input": 1_000_000,
    "output": 1_000_000,
    "cache_creation": 0,
    "cache_read": 0,
}


class EstimateCostTest(unittest.TestCase):
    def test_known_model_sums_input_and_output(self):
        with patch.dict(PRICES, {"test-model": ModelPrice(
            input=10.0, output=20.0, cache_write=0.0, cache_read=0.0
        )}):
            self.assertAlmostEqual(estimate_cost("test-model", TOKENS), 30.0)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(estimate_cost("no-such-model", TOKENS))

    def test_none_model_returns_none(self):
        self.assertIsNone(estimate_cost(None, TOKENS))

    def test_cache_read_priced_separately_from_input(self):
        tokens = {"input": 0, "output": 0, "cache_creation": 0,
                  "cache_read": 1_000_000}
        with patch.dict(PRICES, {"cheap-cache": ModelPrice(
            input=10.0, output=0.0, cache_write=0.0, cache_read=1.0
        )}):
            # Priced as cache_read (1.0), NOT as input (10.0).
            self.assertAlmostEqual(estimate_cost("cheap-cache", tokens), 1.0)

    def test_every_shipped_price_is_complete(self):
        for name, price in PRICES.items():
            for field in ("input", "output", "cache_write", "cache_read"):
                self.assertIsInstance(getattr(price, field), float, name)


if __name__ == "__main__":
    unittest.main()
