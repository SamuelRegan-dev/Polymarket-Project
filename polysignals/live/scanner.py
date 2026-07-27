"""
Live scanner — does insider-adjacent activity exist in the target
Polymarket markets that are closing soon?

Run this on a cron or manually (python main.py live). Each run:
  1. Fetches the cohort's markets closing within SIGNAL_WINDOW_HOURS
  2. Fetches their trades
  3. Runs both threshold detectors
  4. Writes all findings and per-market summaries to DuckDB

Nothing is filtered out silently — every market that enters the
universe gets a summary row, whether or not a signal fires.
"""

import logging
import time

import requests

from polysignals.config import DEFAULT_COHORT, SIGNAL_WINDOW_HOURS
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

    markets_scanned = 0
    markets_in_window = 0
    total_flagged = 0

    logger.info(
        "Scanning %s markets closing within %sh...", cohort, SIGNAL_WINDOW_HOURS
    )

    for market in polymarket_api.iter_cohort_markets(cohort, closed=False):
        markets_scanned += 1

        # Pre-flight: skip if no end date or already outside our window
        end_date_str = market.get("endDate") or market.get("endDateIso")
        end_unix = sig._parse_iso_to_unix(end_date_str) if end_date_str else None

        if end_unix is None:
            logger.debug("Skipping '%s' — no end date", market.get("question", "?")[:60])
            continue

        hours_remaining = (end_unix - time.time()) / 3600

        if not (0 < hours_remaining <= SIGNAL_WINDOW_HOURS):
            logger.debug(
                "Outside window (%.1fh remaining): %s",
                hours_remaining,
                market.get("question", "?")[:60],
            )
            continue

        markets_in_window += 1
        question = market.get("question", "?")[:80]
        logger.info("→ In window (%.1fh): %s", hours_remaining, question)

        # Fetch trades — can fail if a market is new or illiquid
        try:
            raw_trades = polymarket_api.fetch_trades_for_market(market)
        except requests.HTTPError as exc:
            logger.warning("Failed to fetch trades: %s", exc)
            continue

        if not raw_trades:
            logger.info("  No trades found.")
            continue

        logger.info("  %d total trades fetched.", len(raw_trades))

        # Detect signals
        flagged = sig.detect_signals(market, raw_trades)
        summary = sig.summarise_window(market, raw_trades)

        if flagged:
            total_flagged += len(flagged)
            for ft in flagged:
                tag = []
                if ft.flat_threshold_hit: tag.append("FLAT")
                if ft.pct_threshold_hit:  tag.append("PCT")
                logger.info(
                    "  [%s] $%.0f | %.1fh before close | outcome=%s | amihud=%s",
                    "+".join(tag),
                    ft.trade.volume_usd,
                    ft.hours_before_close,
                    ft.trade.outcome,
                    f"{ft.amihud_ratio:.2e}" if ft.amihud_ratio is not None else "n/a",
                )
            storage.insert_flagged_trades(flagged, cohort=cohort)
        else:
            logger.info("  No threshold hits.")

        storage.insert_market_summary(summary, cohort=cohort)

        # Polite rate limiting
        time.sleep(0.2)

    logger.info(
        "Done. Scanned=%d  In-window=%d  Flagged=%d",
        markets_scanned, markets_in_window, total_flagged,
    )


if __name__ == "__main__":
    run()
