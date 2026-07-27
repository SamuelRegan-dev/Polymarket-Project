"""
Gamma-API resolution cross-check.

Looks up the Polymarket Gamma API directly — by market id and, where
needed, by condition id — for every market already stored in the local
`markets` table, and writes the resolved winning outcome back into the
same DuckDB database.

Why this is a third, independent source of "winner":
  - enrich_markets.py     → heuristic. Infers a winner from the last trade
                            price in the orderFilled CSV. Not a real
                            resolution, just a local approximation.
  - fetch_resolutions.py  → on-chain ground truth (payoutNumerators read
                            from the ConditionalTokens contract). Writes
                            markets.onchain_winner.
  - THIS FILE             → Gamma's own resolution (outcomePrices +
                            umaResolutionStatus), written to separate
                            gamma_* columns so all three sources can be
                            compared against each other.

How resolution is determined (verified against Gamma's live OpenAPI spec,
docs.polymarket.com/api-reference/markets/get-market-by-id — there is no
dedicated "winner" field):
    umaResolutionStatus == "resolved"
    AND one entry in outcomePrices >= RESOLVED_PRICE_THRESHOLD
    → the matching entry in `outcomes` is the winner.
A high price alone (a near-certain but not-yet-resolved market — which is
the exact phenomenon this project is trying to detect) does NOT count as
resolved. Rare 50/50 UMA rulings resolve with no single outcome crossing
the threshold; that case is stored as gamma_uma_status='resolved' with
gamma_winning_outcome=NULL, distinct from "not resolved yet"
(gamma_uma_status IS NULL/'proposed'/'disputed').

Batching: GET /markets accepts repeated `id=` and `condition_ids=` params
(confirmed in Gamma's OpenAPI spec), so this fetches MARKETS_PAGE_SIZE
markets per request instead of one request per market. Gamma's own
documented limit is 300 req/10s on /markets — this stays far under that.

Safe to re-run: a market already confirmed gamma_uma_status='resolved' is
skipped on the next run. Everything else (never checked, still open,
proposed, disputed) is retried, since those can still change.

Adds three columns to `markets` the first time it runs (ALTER TABLE ...
ADD COLUMN IF NOT EXISTS, so re-running is harmless):
    gamma_winning_outcome VARCHAR
    gamma_uma_status      VARCHAR
    gamma_checked_at      TIMESTAMP

Reference:
    https://docs.polymarket.com/api-reference/markets/get-market-by-id
    https://docs.polymarket.com/api-reference/markets/list-markets
    https://docs.polymarket.com/api-reference/rate-limits
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import duckdb
import requests

from polysignals.config import (
    DB_PATH,
    GAMMA_API,
    MARKETS_PAGE_SIZE,
    REQUEST_TIMEOUT,
    RESOLVED_PRICE_THRESHOLD,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

REQUEST_PAUSE_SECONDS = 0.25  # per chunk request; Gamma allows 300 req/10s on /markets


# ── Schema ────────────────────────────────────────────────────────────────────

def _ensure_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add the three Gamma-resolution columns to `markets` if not already present."""
    conn.execute("ALTER TABLE markets ADD COLUMN IF NOT EXISTS gamma_winning_outcome VARCHAR")
    conn.execute("ALTER TABLE markets ADD COLUMN IF NOT EXISTS gamma_uma_status VARCHAR")
    conn.execute("ALTER TABLE markets ADD COLUMN IF NOT EXISTS gamma_checked_at TIMESTAMP")


# ── Gamma API ─────────────────────────────────────────────────────────────────

def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _fetch_markets_by(param_name: str, ids: list[str]) -> list[dict]:
    """
    One batched GET /markets call using id=... or condition_ids=... (both
    are repeatable array params per Gamma's OpenAPI spec). Missing ids are
    just absent from the result — Gamma doesn't error on unknown ids.
    """
    if not ids:
        return []
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={param_name: ids},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("  Batch lookup failed (%s, %d ids): %s", param_name, len(ids), exc)
        return []

    data = resp.json()
    return data if isinstance(data, list) else []


def _json_list(raw, default: list) -> list:
    if raw is None:
        return default
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def extract_resolution(market: dict) -> tuple[str | None, str | None]:
    """
    Returns (winning_outcome, uma_status).

    winning_outcome is set only when umaResolutionStatus == "resolved" AND
    one outcome price has crossed RESOLVED_PRICE_THRESHOLD.
    """
    uma_status = market.get("umaResolutionStatus")
    outcomes = _json_list(market.get("outcomes"), default=[])
    prices_raw = _json_list(market.get("outcomePrices"), default=[])

    try:
        prices = [float(p) for p in prices_raw]
    except (TypeError, ValueError):
        prices = []

    winner = None
    if uma_status == "resolved":
        for outcome, price in zip(outcomes, prices):
            if price >= RESOLVED_PRICE_THRESHOLD:
                winner = outcome
                break

    return winner, uma_status


# ── Main ──────────────────────────────────────────────────────────────────────

def _markets_table_exists(conn: duckdb.DuckDBPyConnection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'markets'"
        ).fetchall()
    )


def run() -> None:
    conn = duckdb.connect(DB_PATH)

    if not _markets_table_exists(conn):
        logger.error(
            "No `markets` table found in %s — run `python main.py load` "
            "first to populate it.",
            DB_PATH,
        )
        conn.close()
        return

    _ensure_columns(conn)

    rows = conn.execute(
        """
        SELECT market_id
        FROM   markets
        WHERE  gamma_uma_status IS NULL
           OR  gamma_uma_status != 'resolved'
        ORDER  BY market_id
        """
    ).fetchall()
    market_ids = [r[0] for r in rows if r[0]]

    logger.info("Markets to check: %d", len(market_ids))

    if not market_ids:
        logger.info("Nothing to do — every market already confirmed resolved.")
        conn.close()
        return

    # Route by what market_id looks like. insert_markets() (core/storage.py)
    # only falls back to condition_id as market_id when the source CSV had
    # no Gamma `id` for that row — so a non-numeric market_id must BE a
    # condition id, and needs the condition_ids= filter instead of id=.
    numeric_ids = [m for m in market_ids if m.isdigit()]
    non_numeric_ids = [m for m in market_ids if not m.isdigit()]

    if non_numeric_ids:
        logger.info(
            "%d market_id(s) aren't plain numeric Gamma ids — "
            "looking those up by condition_id instead.",
            len(non_numeric_ids),
        )

    found: dict[str, dict] = {}  # market_id -> market dict, keyed to match rows above

    for chunk in _chunked(numeric_ids, MARKETS_PAGE_SIZE):
        for market in _fetch_markets_by("id", chunk):
            mid = str(market.get("id", ""))
            if mid:
                found[mid] = market
        time.sleep(REQUEST_PAUSE_SECONDS)

    for chunk in _chunked(non_numeric_ids, MARKETS_PAGE_SIZE):
        for market in _fetch_markets_by("condition_ids", chunk):
            cid = market.get("conditionId", "")
            if cid:
                found[cid] = market
        time.sleep(REQUEST_PAUSE_SECONDS)

    logger.info("Gamma returned data for %d / %d requested markets.", len(found), len(market_ids))

    now = datetime.now(timezone.utc)
    resolved = still_open = missing = 0

    for market_id in market_ids:
        market = found.get(market_id)

        if market is None:
            missing += 1
            continue

        winner, uma_status = extract_resolution(market)

        conn.execute(
            """
            UPDATE markets
            SET gamma_winning_outcome = ?,
                gamma_uma_status      = ?,
                gamma_checked_at      = ?
            WHERE market_id = ?
            """,
            [winner, uma_status, now, market_id],
        )

        if winner is not None:
            resolved += 1
        else:
            still_open += 1

    conn.close()

    logger.info("=" * 60)
    logger.info("GAMMA RESOLUTION CHECK COMPLETE")
    logger.info("  Checked            : %d", len(market_ids))
    logger.info("  Resolved (winner)  : %d", resolved)
    logger.info("  Open/unresolved    : %d", still_open)
    logger.info("  Not found on Gamma : %d", missing)
    logger.info("=" * 60)

    if missing:
        logger.info(
            "Not-found markets are left untouched (gamma_uma_status stays NULL) "
            "and will be retried on the next run."
        )


if __name__ == "__main__":
    run()
