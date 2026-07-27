"""
API-driven backtest — the same signal detectors, but run over historical
resolved markets fetched live from the Polymarket APIs instead of the
local CSV dumps. Useful when the CSVs aren't available; slower and capped
by API pagination limits.

Each run:
  1. Fetches the cohort's resolved markets (back to BACKTEST_START_CUTOFF)
  2. Fetches their trades
  3. Runs both threshold detectors
  4. Tags each flagged trade with the actual winning outcome
  5. Writes all findings and per-market summaries to DuckDB

Nothing is filtered out silently — every market that enters the
universe gets a summary row, whether or not a signal fires.
"""

import logging
import time

import requests

from polysignals.config import DEFAULT_COHORT, MIN_RESOLVED_SAMPLE
from polysignals.core import polymarket_api, storage
from polysignals.core import signals as sig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run(cohort: str = DEFAULT_COHORT) -> None:
    storage.initialise()

    markets_scanned   = 0
    markets_with_data = 0
    total_flagged     = 0
    correct           = 0
    wrong             = 0

    logger.info("Starting backtest — fetching all resolved %s markets...", cohort)

    for market in polymarket_api.iter_resolved_cohort_markets(cohort):
        markets_scanned += 1
        question = market.get("question", "?")[:80]
        winner   = polymarket_api.get_winning_outcome(market)

        logger.info("→ %s  |  winner=%s", question, winner)

        # Fetch trades
        try:
            raw_trades = polymarket_api.fetch_trades_for_market(market)
        except requests.HTTPError as exc:
            logger.warning("  Failed to fetch trades: %s", exc)
            continue

        if not raw_trades:
            logger.info("  No trades found.")
            storage.insert_market_summary(sig.summarise_window(market, []), cohort=cohort)
            continue

        markets_with_data += 1
        logger.info("  %d total trades fetched.", len(raw_trades))

        # Detect signals
        flagged = sig.detect_signals(market, raw_trades)
        summary = sig.summarise_window(market, raw_trades)

        # Tag each flagged trade with outcome correctness
        for ft in flagged:
            ft.winning_outcome    = winner
            ft.trade_was_correct  = (winner == ft.trade.outcome) if winner else None

            if ft.trade_was_correct is True:
                correct += 1
            elif ft.trade_was_correct is False:
                wrong += 1

        if flagged:
            total_flagged += len(flagged)
            for ft in flagged:
                tags = []
                if ft.flat_threshold_hit: tags.append("FLAT")
                if ft.pct_threshold_hit:  tags.append("PCT")
                logger.info(
                    "  [%s] $%.0f | %.1fh before close | outcome=%s | winner=%s | correct=%s",
                    "+".join(tags),
                    ft.trade.volume_usd,
                    ft.hours_before_close,
                    ft.trade.outcome,
                    winner,
                    ft.trade_was_correct,
                )
            storage.insert_flagged_trades(flagged, cohort=cohort)
        else:
            logger.info("  No threshold hits.")

        storage.insert_market_summary(summary, cohort=cohort)
        time.sleep(0.2)

    # ── Summary ───────────────────────────────────────────────────────────────
    total_resolved = correct + wrong
    hit_rate = (correct / total_resolved * 100) if total_resolved > 0 else 0.0

    logger.info("=" * 60)
    logger.info("BACKTEST COMPLETE  [cohort=%s]", cohort)
    logger.info("  Markets scanned   : %d", markets_scanned)
    logger.info("  Markets with data : %d", markets_with_data)
    logger.info("  Flagged trades    : %d", total_flagged)
    logger.info("  Resolved flags    : %d", total_resolved)
    logger.info("  Correct direction : %d  (%.1f%%)", correct, hit_rate)
    logger.info("  Wrong direction   : %d", wrong)
    logger.info("=" * 60)

    if total_resolved < MIN_RESOLVED_SAMPLE:
        logger.warning(
            "Sample too small (%d resolved flags, want >=%d) — results are not "
            "statistically meaningful. Broaden the cohort's keyword list or use "
            "the CSV pipeline.",
            total_resolved, MIN_RESOLVED_SAMPLE,
        )


if __name__ == "__main__":
    run()
