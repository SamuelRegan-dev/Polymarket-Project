"""
Tests for polysignals.core.signals — pure function coverage only.
No network calls. No DB.

Run from the repo root: python -m pytest tests/ -v
"""

import time
import unittest
from datetime import datetime, timezone

from polysignals.config import FLAT_THRESHOLD_USD, SIGNAL_WINDOW_HOURS
from polysignals.core.signals import (
    Trade,
    compute_amihud,
    detect_signals,
    flat_threshold_flag,
    parse_trades,
    pct_threshold_flag,
    trades_in_signal_window,
)


def _make_raw(timestamp, size, price, outcome="Yes", token_id="tok1"):
    return {
        "timestamp": timestamp,
        "size": size,
        "price": price,
        "_outcome": outcome,
        "_token_id": token_id,
    }


def _make_market(end_unix: float, market_id: str = "mkt1") -> dict:
    end_iso = datetime.fromtimestamp(end_unix, tz=timezone.utc).isoformat()
    return {
        "id": market_id,
        "question": "Will Best Picture go to a committee pick?",
        "endDate": end_iso,
        "tokens": [{"token_id": "tok1", "outcome": "Yes"}],
    }


class TestParseTrades(unittest.TestCase):
    def test_valid_rows_parsed(self):
        raw = [_make_raw(1_000_000, 100.0, 0.5)]
        trades = parse_trades(raw)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].volume_usd, 50.0)

    def test_malformed_rows_dropped(self):
        raw = [
            {"timestamp": "bad", "size": "x", "price": 0.5},
            _make_raw(1_000_000, 50.0, 0.6),
        ]
        trades = parse_trades(raw)
        self.assertEqual(len(trades), 1)

    def test_volume_usd_computed(self):
        raw = [_make_raw(1_000_000, 200.0, 0.75)]
        trades = parse_trades(raw)
        self.assertAlmostEqual(trades[0].volume_usd, 150.0)


class TestSignalWindow(unittest.TestCase):
    def setUp(self):
        self.end_unix = 1_700_000_000.0

    def _trade_at(self, seconds_before_end):
        ts = int(self.end_unix - seconds_before_end)
        raw = [_make_raw(ts, 100.0, 0.5)]
        return parse_trades(raw)[0]

    def test_trade_inside_window(self):
        trade = self._trade_at(3600)   # 1h before close
        result = trades_in_signal_window([trade], self.end_unix)
        self.assertEqual(len(result), 1)

    def test_trade_outside_window(self):
        trade = self._trade_at(SIGNAL_WINDOW_HOURS * 3600 + 1)
        result = trades_in_signal_window([trade], self.end_unix)
        self.assertEqual(len(result), 0)

    def test_trade_exactly_at_boundary(self):
        trade = self._trade_at(SIGNAL_WINDOW_HOURS * 3600)
        result = trades_in_signal_window([trade], self.end_unix)
        self.assertEqual(len(result), 1)


class TestAmihud(unittest.TestCase):
    def _make_trades(self, prices):
        return [Trade(timestamp=i, size=100.0, price=p, outcome="Yes", token_id="t") for i, p in enumerate(prices)]

    def test_first_trade_always_none(self):
        trades = self._make_trades([0.5, 0.6])
        ratios = compute_amihud(trades)
        self.assertIsNone(ratios["0"])

    def test_ratio_computed_correctly(self):
        # |0.6 - 0.5| / (100 * 0.6) = 0.1 / 60
        trades = self._make_trades([0.5, 0.6])
        ratios = compute_amihud(trades)
        self.assertAlmostEqual(ratios["1"], 0.1 / 60.0, places=8)

    def test_zero_volume_handled(self):
        trades = [
            Trade(timestamp=0, size=0.0, price=0.5, outcome="Yes", token_id="t"),
            Trade(timestamp=1, size=0.0, price=0.6, outcome="Yes", token_id="t"),
        ]
        ratios = compute_amihud(trades)
        self.assertIsNone(ratios["1"])


class TestThresholds(unittest.TestCase):
    def _trade(self, volume_usd: float) -> Trade:
        size = volume_usd / 0.5
        return Trade(timestamp=0, size=size, price=0.5, outcome="Yes", token_id="t")

    def test_flat_threshold_hit(self):
        self.assertTrue(flat_threshold_flag(self._trade(FLAT_THRESHOLD_USD)))
        self.assertTrue(flat_threshold_flag(self._trade(FLAT_THRESHOLD_USD + 1)))

    def test_flat_threshold_miss(self):
        self.assertFalse(flat_threshold_flag(self._trade(FLAT_THRESHOLD_USD - 1)))

    def test_pct_threshold_top10(self):
        # 10 trades; top 1 should hit
        trades = [self._trade(v) for v in [100, 200, 300, 400, 500, 600, 700, 800, 900, 50_000]]
        big_trade = trades[-1]
        self.assertTrue(pct_threshold_flag(big_trade, trades))

    def test_pct_threshold_bottom_miss(self):
        trades = [self._trade(v) for v in [100, 200, 300, 400, 500, 600, 700, 800, 900, 50_000]]
        small_trade = trades[0]
        self.assertFalse(pct_threshold_flag(small_trade, trades))


class TestDetectSignals(unittest.TestCase):
    def test_no_end_date_returns_empty(self):
        market = {"id": "x", "question": "test", "tokens": []}
        self.assertEqual(detect_signals(market, []), [])

    def test_market_outside_window_returns_empty(self):
        far_future = time.time() + (SIGNAL_WINDOW_HOURS * 3600 * 10)
        market = _make_market(far_future)
        ts = int(far_future - 3600)
        raw = [_make_raw(ts, 1_000_000.0, 0.5)]
        self.assertEqual(detect_signals(market, raw), [])

    def test_large_trade_in_window_flagged(self):
        closing_soon = time.time() + 3600   # closes in 1h
        market = _make_market(closing_soon)
        ts = int(closing_soon - 1800)       # trade 30min before close
        size = (FLAT_THRESHOLD_USD * 2) / 0.5
        raw = [_make_raw(ts, size, 0.5)]
        flagged = detect_signals(market, raw)
        self.assertEqual(len(flagged), 1)
        self.assertTrue(flagged[0].flat_threshold_hit)
        # Resolution fields default to None until a winner is known
        self.assertIsNone(flagged[0].winning_outcome)
        self.assertIsNone(flagged[0].trade_was_correct)


if __name__ == "__main__":
    unittest.main()
