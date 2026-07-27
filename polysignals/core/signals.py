"""
Signal detection logic.

Pure functions only — no API calls, no I/O, no side effects.
Each function takes raw trade lists and returns structured findings.

Two thresholds tested in parallel per the research brief:
  [1] Flat dollar threshold (FLAT_THRESHOLD_USD)
  [2] Top-N% by volume within the market (TOP_PCT_THRESHOLD)
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone

from polysignals.config import FLAT_THRESHOLD_USD, TOP_PCT_THRESHOLD, SIGNAL_WINDOW_HOURS


@dataclass
class Trade:
    """Normalised trade record. All dollar values in USD."""
    timestamp:   int
    size:        float
    price:       float
    outcome:     str
    token_id:    str
    volume_usd:  float = field(init=False)

    def __post_init__(self) -> None:
        self.volume_usd = self.size * self.price


@dataclass
class FlaggedTrade:
    trade:             Trade
    market_id:         str
    market_question:   str
    hours_before_close: float
    flat_threshold_hit: bool
    pct_threshold_hit:  bool
    amihud_ratio:       float | None    # |Δprice| / volume_usd, None if no prior trade
    # Resolution fields are filled in later, once the market's winner is
    # known — detect_signals() itself can't know them, so they default.
    winning_outcome: str | None = None
    trade_was_correct: bool | None = None


def parse_trades(raw_trades: list[dict]) -> list[Trade]:
    """Convert raw API dicts to Trade objects, dropping malformed rows."""
    parsed = []
    for t in raw_trades:
        try:
            parsed.append(Trade(
                timestamp = int(t["timestamp"]),
                size      = float(t["size"]),
                price     = float(t["price"]),
                outcome   = t.get("_outcome", "unknown"),
                token_id  = t.get("_token_id", ""),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return parsed


def trades_in_signal_window(
    trades: list[Trade],
    end_unix: float,
    window_hours: float = SIGNAL_WINDOW_HOURS,
) -> list[Trade]:
    """Return only trades that fall within [close - window, close]."""
    window_start = end_unix - (window_hours * 3600)
    return [t for t in trades if window_start <= t.timestamp <= end_unix]


def compute_amihud(trades: list[Trade]) -> dict[str, float | None]:
    """
    Amihud illiquidity ratio per trade: |Δprice| / volume_usd.

    Requires at least two consecutive trades. Returns a dict keyed by
    trade index (as string) → ratio. First trade always gets None.
    """
    ratios: dict[str, float | None] = {"0": None}
    for i in range(1, len(trades)):
        prev, curr = trades[i - 1], trades[i]
        delta = abs(curr.price - prev.price)
        if curr.volume_usd > 0:
            ratios[str(i)] = delta / curr.volume_usd
        else:
            ratios[str(i)] = None
    return ratios


def flat_threshold_flag(trade: Trade) -> bool:
    return trade.volume_usd >= FLAT_THRESHOLD_USD


def pct_threshold_flag(trade: Trade, all_window_trades: list[Trade]) -> bool:
    """True if this trade is in the top TOP_PCT_THRESHOLD by volume."""
    if not all_window_trades:
        return False
    volumes = sorted([t.volume_usd for t in all_window_trades], reverse=True)
    cutoff_index = max(1, int(len(volumes) * TOP_PCT_THRESHOLD))
    cutoff = volumes[cutoff_index - 1]
    return trade.volume_usd >= cutoff


def detect_signals(
    market: dict,
    raw_trades: list[dict],
) -> list[FlaggedTrade]:
    """
    Main entry point.

    Given a market dict and its raw trade list, return all trades
    in the signal window that hit either threshold.
    """
    end_date_str = market.get("endDate") or market.get("endDateIso")
    if not end_date_str:
        return []

    end_unix = _parse_iso_to_unix(end_date_str)
    if end_unix is None:
        return []

    now_unix = datetime.now(timezone.utc).timestamp()

    # Only scan markets closing within SIGNAL_WINDOW_HOURS from now
    if not (0 < end_unix - now_unix <= SIGNAL_WINDOW_HOURS * 3600):
        return []

    trades = parse_trades(raw_trades)
    window_trades = trades_in_signal_window(trades, end_unix)

    if not window_trades:
        return []

    amihud_map = compute_amihud(window_trades)
    flagged: list[FlaggedTrade] = []

    for i, trade in enumerate(window_trades):
        flat_hit = flat_threshold_flag(trade)
        pct_hit  = pct_threshold_flag(trade, window_trades)

        if not (flat_hit or pct_hit):
            continue

        hours_before = (end_unix - trade.timestamp) / 3600

        flagged.append(FlaggedTrade(
            trade              = trade,
            market_id          = market.get("id") or market.get("conditionId", ""),
            market_question    = market.get("question", ""),
            hours_before_close = hours_before,
            flat_threshold_hit = flat_hit,
            pct_threshold_hit  = pct_hit,
            amihud_ratio       = amihud_map.get(str(i)),
        ))

    return flagged


def summarise_window(market: dict, raw_trades: list[dict]) -> dict:
    """
    Aggregate stats for a market's signal window.
    Useful for storing baseline data even when no threshold is hit.
    """
    end_date_str = market.get("endDate") or market.get("endDateIso")
    end_unix = _parse_iso_to_unix(end_date_str) if end_date_str else None

    if end_unix is None:
        return {}

    trades = parse_trades(raw_trades)
    window_trades = trades_in_signal_window(trades, end_unix)

    if not window_trades:
        return {"market_id": market.get("id"), "trade_count": 0}

    volumes = [t.volume_usd for t in window_trades]
    return {
        "market_id":       market.get("id") or market.get("conditionId"),
        "question":        market.get("question"),
        "trade_count":     len(window_trades),
        "total_volume":    sum(volumes),
        "max_volume":      max(volumes),
        "median_volume":   statistics.median(volumes),
        "p90_volume":      _percentile(volumes, 90),
        "flat_hits":       sum(1 for t in window_trades if flat_threshold_flag(t)),
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_iso_to_unix(dt_str: str) -> float | None:
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, AttributeError):
        return None


def _percentile(data: list[float], p: int) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    index = (p / 100) * (len(sorted_data) - 1)
    lower, upper = int(index), min(int(index) + 1, len(sorted_data) - 1)
    return sorted_data[lower] + (index - lower) * (sorted_data[upper] - sorted_data[lower])
