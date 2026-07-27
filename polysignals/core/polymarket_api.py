"""
Thin wrappers around Polymarket API endpoints.
No signal logic here — just fetch and return raw dicts.

Raises requests.HTTPError on non-2xx responses so callers can decide
whether to retry or skip.

Market universe: whichever cohort the caller asks for — see
core/cohorts.py. Cohort membership is decided by question text; noise is
filtered by requiring minimum volume and valid token structure.
"""

import json
import logging
import time
from typing import Iterator

import requests

from polysignals.config import (
    GAMMA_API, DATA_API,
    BACKTEST_START_CUTOFF,
    DEFAULT_COHORT,
    MARKETS_PAGE_SIZE, TRADES_PAGE_SIZE,
    MAX_TRADE_OFFSET,
    REQUEST_TIMEOUT,
    RESOLVED_PRICE_THRESHOLD,
    MIN_MARKET_VOLUME,
)
from polysignals.core import cohorts

logger = logging.getLogger(__name__)


# ── Markets ───────────────────────────────────────────────────────────────────

def iter_cohort_markets(cohort: str = DEFAULT_COHORT, closed: bool = False) -> Iterator[dict]:
    """
    Scan all active markets and yield those belonging to `cohort`.
    Paginates automatically.

    Two-stage filter:
      1. Exclusive cohort match on question text (see core/cohorts.py)
      2. Quality filter (drops illiquid/malformed markets)
    """
    cohorts.keywords_for(cohort)  # fail fast on a typo'd cohort name
    offset = 0
    while True:
        params = {
            "closed":    str(closed).lower(),
            "active":    "true",
            "limit":     MARKETS_PAGE_SIZE,
            "offset":    offset,
            "order":     "volume24hr",
            "ascending": "false",
        }
        resp = _get(f"{GAMMA_API}/markets", params=params)
        batch: list[dict] = resp.json()

        if not batch:
            break

        for market in batch:
            if _is_cohort_market(market, cohort) and _is_quality_market(market):
                yield market

        if len(batch) < MARKETS_PAGE_SIZE:
            break

        offset += MARKETS_PAGE_SIZE


def _is_cohort_market(market: dict, cohort: str) -> bool:
    """
    True if the question belongs to `cohort` and to no other cohort.

    Exclusive on purpose: a market matching two universes would otherwise
    end up in both arms of the counter-test. See core/cohorts.py.
    """
    return cohorts.is_in_cohort(market.get("question") or "", cohort)


def _is_quality_market(market: dict) -> bool:
    tokens = market.get("tokens") or []
    has_valid_token = any(t.get("token_id") for t in tokens)

    if not has_valid_token:
        raw = market.get("clobTokenIds", "[]")
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            has_valid_token = len(ids) > 0
        except Exception:
            pass

    if not has_valid_token:
        return False

    volume = float(market.get("volume") or 0)
    if volume < MIN_MARKET_VOLUME:
        return False

    if not (market.get("endDate") or market.get("endDateIso")):
        return False

    return True


# ── Trades ────────────────────────────────────────────────────────────────────

def fetch_trades_for_token(token_id: str) -> list[dict]:
    """
    Fetch all available trades for a single outcome token, sorted ascending
    by timestamp so price-impact calculations work correctly.

    The Data API paginates at 500 rows. We exhaust it.
    Returns an empty list if the market has no recorded trades.
    """

    all_trades: list[dict] = []
    offset = 0

    while True:
        if offset > MAX_TRADE_OFFSET:
            logger.warning("Offset cap reached for token %s — truncating at %d trades", token_id, len(all_trades))
            break
        params = {
            "asset_id": token_id,
            "limit":    TRADES_PAGE_SIZE,
            "offset":   offset,
        }
        resp = _get(f"{DATA_API}/trades", params=params)
        batch: list[dict] = resp.json()

        if not batch:
            break

        all_trades.extend(batch)

        if len(batch) < TRADES_PAGE_SIZE:
            break

        offset += TRADES_PAGE_SIZE
        time.sleep(0.1)

    all_trades.sort(key=lambda t: int(t.get("timestamp") or 0))
    return all_trades


def fetch_trades_for_market(market: dict) -> list[dict]:
    tokens = _market_tokens(market)

    merged: list[dict] = []
    for token in tokens:
        token_id = token.get("token_id")
        if not token_id:
            continue
        trades = fetch_trades_for_token(token_id)
        for t in trades:
            t["_outcome"]   = token.get("outcome", "unknown")
            t["_token_id"]  = token_id
        merged.extend(trades)

    merged.sort(key=lambda t: int(t.get("timestamp") or 0))
    return merged


def iter_resolved_cohort_markets(cohort: str = DEFAULT_COHORT) -> Iterator[dict]:
    cohorts.keywords_for(cohort)  # fail fast on a typo'd cohort name
    CUTOFF = BACKTEST_START_CUTOFF

    seen: set[str] = set()
    total_scanned = 0
    total_matched = 0
    last_closed_time: str | None = None
    page = 0

    while True:
        params = {
            "closed":    "true",
            "limit":     MARKETS_PAGE_SIZE,
            "order":     "closedTime",
            "ascending": "false",
        }
        if last_closed_time:
            params["endDate"] = last_closed_time

        resp = _get(f"{GAMMA_API}/markets", params=params)
        batch = resp.json()

        if not isinstance(batch, list) or not batch:
            break

        page += 1
        stop_early = False

        for market in batch:
            if not isinstance(market, dict):
                continue

            closed_time = market.get("closedTime") or ""
            if closed_time and closed_time < CUTOFF:
                logger.info("Reached 2022 cutoff at %s — stopping.", closed_time)
                stop_early = True
                break

            mid = market.get("id") or market.get("conditionId")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            total_scanned += 1

            in_cohort    = _is_cohort_market(market, cohort)
            is_quality   = _is_quality_market(market)
            is_resolved  = _is_resolved(market)

            if total_scanned % 250 == 0:
                logger.info("  [page %d] scanned %d | matched %d", page, total_scanned, total_matched)

            if in_cohort and is_quality and is_resolved:
                total_matched += 1
                yield market

        if stop_early or len(batch) < MARKETS_PAGE_SIZE:
            break

        prev = last_closed_time
        last_closed_time = next(
            (m.get("closedTime") for m in reversed(batch) if isinstance(m, dict) and m.get("closedTime")),
            None
        )
        if not last_closed_time:
            logger.error("No closedTime found in batch — stopping.")
            break
        if last_closed_time == prev and len(batch) < MARKETS_PAGE_SIZE:
            break

    pct = (total_matched / total_scanned * 100) if total_scanned else 0
    logger.info("=" * 60)
    logger.info("SCAN COMPLETE")
    logger.info("  Total scanned : %d", total_scanned)
    logger.info("  Matched       : %d", total_matched)
    logger.info("  Match rate    : %.2f%%", pct)
    logger.info("=" * 60)


def _is_resolved(market: dict) -> bool:
    if market.get("umaResolutionStatus") != "resolved":
        return False
    prices = _parse_outcome_prices(market)
    return any(p >= RESOLVED_PRICE_THRESHOLD for p in prices)


def get_winning_outcome(market: dict) -> str | None:
    tokens = _market_tokens(market)
    prices = _parse_outcome_prices(market)

    for token, price in zip(tokens, prices):
        if price >= RESOLVED_PRICE_THRESHOLD:
            return token.get("outcome")

    return None


# ── Internal helpers ──────────────────────────────────────────────────────────

def _market_tokens(market: dict) -> list[dict]:
    """Token list for a market, rebuilt from clobTokenIds/outcomes when the
    Gamma payload has no explicit tokens array."""
    tokens = market.get("tokens") or []
    if tokens:
        return tokens

    raw_ids      = market.get("clobTokenIds", "[]")
    raw_outcomes = market.get("outcomes", '["Yes", "No"]')
    try:
        ids      = json.loads(raw_ids)      if isinstance(raw_ids, str)      else raw_ids
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        return [
            {"token_id": tid, "outcome": outcomes[i] if i < len(outcomes) else "unknown"}
            for i, tid in enumerate(ids)
        ]
    except Exception:
        return []


def _parse_outcome_prices(market: dict) -> list[float]:
    raw = market.get("outcomePrices", "[]")
    try:
        return [float(p) for p in (raw if isinstance(raw, list) else json.loads(raw))]
    except Exception:
        return []


def _get(url: str, params: dict | None = None) -> requests.Response:
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        logger.warning("HTTP %s for %s — %s", resp.status_code, url, resp.text[:200])
    resp.raise_for_status()
    return resp
