"""
CSV loader.

Joins the raw orderFilled trade CSV against markets.csv, filters to one
cohort's market universe, runs historical signal detection, and writes all
results to DuckDB. The database persists across runs — see
storage.initialise().

The cohort is a parameter, not a constant. The counter-test runs this
exact code over a control universe (sports markets, where insider
information is implausible) and compares the result against the original
committee arm. Both arms MUST run identical detection logic or the
comparison measures nothing — which is why this is parameterised rather
than copied into a second loader that could drift out of sync.

Pipeline position (python main.py runs all of this in order):
  1. this file             — populates the DuckDB store        (main.py load)
  2. fetch_resolutions.py  — on-chain winners via Polygon      (main.py resolve)
  3. hit_rate_report()     — directional-accuracy stats        (main.py stats)
  4. analysis.py           — full breakdown queries            (main.py analysis)
  5. compare.py            — cohort-vs-cohort counter-test     (main.py compare)

Key difference from the live scanner:
  detect_signals() in core/signals.py refuses to run on resolved markets
  (it checks whether the market closes in the future). This file calls
  the underlying signal functions directly to bypass that check.
"""

import logging
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from polysignals.config import (
    CSV_CHUNK_SIZE,
    DEFAULT_COHORT,
    MARKETS_CSV_PATH,
    MIN_RESOLVED_SAMPLE,
    ORDERFILLED_CSV_PATH,
    SIGNAL_WINDOW_HOURS,
    TOKEN_DECIMALS,
)
from polysignals.core import cohorts, storage
from polysignals.core.signals import (
    FlaggedTrade,
    _parse_iso_to_unix,
    compute_amihud,
    flat_threshold_flag,
    parse_trades,
    pct_threshold_flag,
    summarise_window,
    trades_in_signal_window,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Trade-leg columns straight from the OrderFilled event schema. Not run
# through the fuzzy _detect_columns() matching below because these exact
# names are what the source data uses; kept optional (checked with
# `in`) in case a given export ever omits one.
TRADE_LEG_COLUMNS = ("makerAssetId", "takerAssetId", "makerAmountFilled", "takerAmountFilled")


# ── Timing helper ─────────────────────────────────────────────────────────────

@contextmanager
def _timed(label: str):
    """Logs how long the wrapped block took (even if it raises), so a
    slow/stalled phase shows up in the logs instead of going silent."""
    start = time.time()
    try:
        yield
    finally:
        logger.info("  ⏱ %s: %.1fs", label, time.time() - start)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_columns(columns: list[str]) -> dict[str, str]:
    """
    Map standard field names to the actual column names present in the CSV.
    Raises ValueError with a clear message if a required column is missing.
    """
    cols = {c.lower().replace(" ", "").replace("_", ""): c for c in columns}

    def find(field_name: str, candidates: list[str]) -> str:
        for c in candidates:
            if c.lower().replace("_", "") in cols:
                return cols[c.lower().replace("_", "")]
        raise ValueError(
            f"Cannot find '{field_name}' column. "
            f"Tried: {candidates}. "
            f"Available columns: {list(columns)}"
        )

    return {
        "timestamp": find("timestamp", [
            "timestamp", "blockTimestamp", "transactionTime",
            "createdAt", "time", "eventTime",
        ]),
        "token_id": find("token_id", [
            "tokenId", "token_id", "assetId", "asset_id",
            "makerAssetId", "takerAssetId",
        ]),
        "size": find("size", [
            "size", "amount", "sizeMatched", "makerAmountFilled",
            "takerAmountFilled", "quantityFilled",
        ])
    }


def _outcome_from_token(token_id: str, market_dict: dict) -> str:
    if str(market_dict.get("token1", "")).strip() == token_id:
        return str(market_dict.get("answer1", "Yes"))
    if str(market_dict.get("token2", "")).strip() == token_id:
        return str(market_dict.get("answer2", "No"))
    return "unknown"


def _detect_historical_signals(
    market_dict: dict,
    raw_trades: list[dict],
) -> list[FlaggedTrade]:
    """
    Signal detection for historical/resolved markets.

    Identical to signals.detect_signals() but without the live-market
    time check (which always returns [] for markets that already closed).
    """
    # Normalise date field — CSV uses closedTime, API uses endDate
    end_date_str = (
        market_dict.get("endDate")
        or market_dict.get("endDateIso")
        or market_dict.get("closedTime")
    )
    if not end_date_str:
        return []

    end_unix = _parse_iso_to_unix(str(end_date_str))
    if end_unix is None:
        return []

    trades        = parse_trades(raw_trades)
    window_trades = trades_in_signal_window(trades, end_unix, SIGNAL_WINDOW_HOURS)

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
            market_id          = market_dict.get("id") or market_dict.get("condition_id", ""),
            market_question    = market_dict.get("question", ""),
            hours_before_close = hours_before,
            flat_threshold_hit = flat_hit,
            pct_threshold_hit  = pct_hit,
            amihud_ratio       = amihud_map.get(str(i)),
            winning_outcome    = market_dict.get("winning_outcome") or None,
            trade_was_correct  = None,   # filled in below
        ))

    return flagged


def _make_summary_dict(market_dict: dict, raw_trades: list[dict]) -> dict:
    """
    summarise_window() looks for endDate/endDateIso; our CSV rows have
    closedTime. Patch the dict before calling so it resolves correctly.
    """
    patched = dict(market_dict)
    if not patched.get("endDate"):
        patched["endDate"] = patched.get("closedTime")
    return summarise_window(patched, raw_trades)


# ── Hit-rate / accuracy report — separate from loading on purpose ─────────────

def hit_rate_report(min_volume: float = 0.0, cohort: str | None = None) -> dict:
    """
    Directional-accuracy check: of the flagged trades already sitting in
    DuckDB, how often did the flagged side match the market's eventual
    winner?

    It's a *check on the data*, not part of loading it — it reads straight
    back from the DB via storage.query_flagged(), so it's fully
    independent of run(): call it any time, as often as you like, without
    re-scanning either CSV.

    Pass a cohort to report on one arm; omit it for everything at once.
    Note this is the naive number — it counts every flagged trade, so
    markets contributing dozens of correlated flags are overweighted. See
    analysis.py for the market-level version, which is the honest one.
    """
    rows = storage.query_flagged(min_volume=min_volume, cohort=cohort)

    correct = sum(1 for r in rows if r.get("trade_was_correct_live") is True)
    wrong = sum(1 for r in rows if r.get("trade_was_correct_live") is False)
    total_resolved = correct + wrong
    hit_rate = (correct / total_resolved * 100) if total_resolved else 0.0

    label = cohort if cohort else "all cohorts"
    logger.info("=" * 60)
    logger.info("HIT-RATE REPORT  [%s]", label)
    logger.info("  Flagged trades loaded : %d", len(rows))
    logger.info("  Resolved flags        : %d", total_resolved)
    logger.info("  Correct direction     : %d  (%.1f%%)", correct, hit_rate)
    logger.info("  Wrong direction       : %d", wrong)
    logger.info("=" * 60)

    if total_resolved < MIN_RESOLVED_SAMPLE:
        logger.warning(
            "Sample too small (%d resolved flags, want >=%d) — not statistically "
            "meaningful.",
            total_resolved, MIN_RESOLVED_SAMPLE,
        )

    return {
        "cohort": cohort,
        "flagged_trades": len(rows),
        "correct": correct,
        "wrong": wrong,
        "total_resolved": total_resolved,
        "hit_rate": hit_rate,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def run(cohort: str = DEFAULT_COHORT) -> None:
    """
    Load one cohort's markets and their in-window flagged trades.

    Safe to run per cohort in any order; each writes only its own rows.
    """
    cohorts.keywords_for(cohort)   # fail fast on a typo'd cohort name
    run_start = time.time()

    logger.info("Loading cohort: %s", cohort)

    for label, path in (("MARKETS_CSV_PATH", MARKETS_CSV_PATH),
                        ("ORDERFILLED_CSV_PATH", ORDERFILLED_CSV_PATH)):
        if not path or not Path(path).exists():
            logger.error(
                "%s is not set or doesn't exist (%r) — set it in .env "
                "(see .env.example).", label, path,
            )
            return

    # ── 1. Initialise DuckDB — idempotent, keeps whatever's already there ────
    storage.initialise()

    # ── 2. Load and filter markets.csv ───────────────────────────────────────
    with _timed("Load + filter markets.csv"):
        logger.info("Reading markets.csv from %s", MARKETS_CSV_PATH)
        markets_df = pd.read_csv(MARKETS_CSV_PATH, dtype=str, low_memory=False)
        logger.info("Total rows in markets.csv: %d", len(markets_df))

        # Cohort assignment is exclusive: a market matching more than one
        # universe is dropped from both. Overlap would put the same market
        # on both sides of the comparison, and would be silently swallowed
        # by the markets PRIMARY KEY on re-insert.
        questions = markets_df["question"].fillna("").tolist()
        distribution = cohorts.cohort_report(questions)

        logger.info("Cohort distribution across markets.csv:")
        for name in cohorts.available():
            logger.info("    %-12s : %d", name, distribution.get(name, 0))
        logger.info("    %-12s : %d", "ambiguous", distribution["_ambiguous"])

        if distribution["_ambiguous"]:
            logger.warning(
                "%d market(s) matched more than one cohort and were excluded "
                "from ALL cohorts. Tighten the keyword lists if this is large.",
                distribution["_ambiguous"],
            )
            for question, hits in cohorts.ambiguous_questions(questions, limit=5):
                logger.warning("    [%s] %s", "+".join(hits), question[:90])

        cohort_df = markets_df[
            markets_df["question"].fillna("").apply(
                lambda q: cohorts.is_in_cohort(q, cohort)
            )
        ].copy()
        logger.info("Markets in cohort %r: %d", cohort, len(cohort_df))

    if cohort_df.empty:
        logger.error(
            "No markets matched cohort %r — check its keyword list in config.py",
            cohort,
        )
        return

    # ── 3. Write cohort markets to DuckDB ────────────────────────────────────
    with _timed("Write markets to DuckDB"):
        storage.insert_markets(cohort_df, cohort=cohort)

    # ── 4. Build token → market lookup ───────────────────────────────────────
    with _timed("Build token→market lookup"):
        token_to_market: dict[str, dict] = {}
        token_to_outcome: dict[str, str] = {}
        for _, row in cohort_df.iterrows():
            market_dict = row.to_dict()
            for col, outcome_col, default in (
                ("token1", "answer1", "Yes"),
                ("token2", "answer2", "No"),
            ):
                tid = str(row.get(col, "")).strip()
                if tid and tid.lower() != "nan":
                    token_to_market[tid] = market_dict
                    token_to_outcome[tid] = str(market_dict.get(outcome_col, default))

        logger.info("Token IDs indexed: %d", len(token_to_market))

    if not token_to_market:
        logger.error("No valid token IDs found in markets.csv — check token1/token2 columns")
        return

    # ── 5. Auto-detect orderFilled.csv columns ────────────────────────────────
    logger.info("Inspecting orderFilled.csv column names...")
    sample = pd.read_csv(ORDERFILLED_CSV_PATH, nrows=5, low_memory=False)
    logger.info("Columns found: %s", list(sample.columns))

    try:
        col_map = _detect_columns(list(sample.columns))
    except ValueError as e:
        logger.error("Column detection failed: %s", e)
        return

    logger.info("Column mapping resolved: %s", col_map)
    token_col = col_map["token_id"]
    ts_col    = col_map["timestamp"]

    # Columns we actually pull per-row, deduped in case token_col/ts_col
    # happen to coincide with one of the maker/taker columns.
    present_trade_cols = [c for c in TRADE_LEG_COLUMNS if c in sample.columns]
    needed_cols = list(dict.fromkeys([token_col, ts_col] + present_trade_cols))

    # ── 6. Scan orderFilled.csv in chunks ────────────────────────────────────
    market_raw_trades: dict[str, list[dict]] = defaultdict(list)
    market_dict_lookup: dict[str, dict]      = {}

    total_rows    = 0
    relevant_rows = 0
    scan_start    = time.time()

    logger.info("Scanning orderFilled.csv (chunk size=%d)...", CSV_CHUNK_SIZE)

    for chunk in pd.read_csv(
        ORDERFILLED_CSV_PATH,
        chunksize=CSV_CHUNK_SIZE,
        dtype=str,
        low_memory=False,
    ):
        total_rows += len(chunk)

        # Vectorised filter down to trades whose token is in a cohort
        # market.
        mask = chunk[token_col].isin(token_to_market)
        n_relevant = int(mask.sum())
        relevant_rows += n_relevant

        if n_relevant:
            # Select rows AND only the columns we need in one shot, then
            # use itertuples() — iterrows() rebuilds a full pandas Series
            # per row and was the main reason this loop was slow on a
            # large CSV.
            narrowed = chunk.loc[mask, needed_cols]

            for row_tuple in narrowed.itertuples(index=False):
                row = row_tuple._asdict()

                token_id = str(row[token_col]).strip()
                market = token_to_market.get(token_id)
                if market is None:
                    continue  # shouldn't happen given the mask, but stay safe
                market_id = str(market.get("id") or market.get("condition_id", ""))

                maker_asset = str(row.get("makerAssetId", "")).strip()
                taker_asset = str(row.get("takerAssetId", "")).strip()
                maker_amt = float(row.get("makerAmountFilled") or 0)
                taker_amt = float(row.get("takerAmountFilled") or 0)

                # price is a ratio of the two legs, so it's unaffected by the
                # shared 6-decimal scaling; size is a raw token amount and must
                # be divided down to real shares (see TOKEN_DECIMALS). Without
                # this, volume_usd = size * price comes out 1e6 too large and
                # the flat threshold flags essentially every trade.
                if taker_asset == "0" and maker_amt > 0:
                    price = taker_amt / maker_amt
                    size = maker_amt / TOKEN_DECIMALS
                elif maker_asset == "0" and taker_amt > 0:
                    price = maker_amt / taker_amt
                    size = taker_amt / TOKEN_DECIMALS
                else:
                    continue

                raw_trade = {
                    "timestamp": row.get(ts_col),
                    "size": size,
                    "price": price,
                    "_outcome": token_to_outcome.get(token_id, "unknown"),
                    "_token_id": token_id,
                }
                market_raw_trades[market_id].append(raw_trade)
                market_dict_lookup[market_id] = market

        if total_rows % 500_000 == 0:
            elapsed = time.time() - scan_start
            rate = total_rows / elapsed if elapsed > 0 else 0.0
            logger.info(
                "  Scanned %dk rows | Relevant so far: %d | %.0f rows/s | %.1fs elapsed",
                total_rows // 1000, relevant_rows, rate, elapsed,
            )

    logger.info(
        "Scan complete — total rows: %d | relevant rows: %d | markets with trades: %d | %.1fs",
        total_rows, relevant_rows, len(market_raw_trades), time.time() - scan_start,
    )

    # ── 7. Run signal detection per market ───────────────────────────────────
    detect_start = time.time()

    total_flagged     = 0
    markets_processed = 0
    all_flagged: list[FlaggedTrade] = []
    all_summaries: list[dict]       = []

    for market_id, raw_trades in market_raw_trades.items():
        market_dict = market_dict_lookup[market_id]
        winner      = market_dict.get("winning_outcome") or None
        if winner and str(winner).lower() == "nan":
            winner = None

        flagged = _detect_historical_signals(market_dict, raw_trades)

        # Direction/correctness is still recorded per-trade — it's part
        # of the persisted schema (flagged_trades.trade_was_correct) —
        # just no longer tallied into a hit-rate % here. See
        # hit_rate_report() for that.
        for ft in flagged:
            ft.winning_outcome   = winner
            ft.trade_was_correct = (winner == ft.trade.outcome) if winner else None

        if flagged:
            total_flagged += len(flagged)
            all_flagged.extend(flagged)

        summary = _make_summary_dict(market_dict, raw_trades)
        if summary:
            all_summaries.append(summary)

        markets_processed += 1

    logger.info("  ⏱ Signal detection: %.1fs", time.time() - detect_start)

    # ── 8. Bulk write results — one connection each, not one per market ──────
    with _timed("Write flagged trades + summaries to DuckDB"):
        storage.insert_flagged_trades(all_flagged, cohort=cohort)
        storage.insert_market_summaries(all_summaries, cohort=cohort)

    # ── 9. Load summary ───────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("CSV LOAD COMPLETE  [cohort=%s]  (%.1fs total)", cohort, time.time() - run_start)
    logger.info("  Cohort markets      : %d", len(cohort_df))
    logger.info("  Markets with trades : %d", markets_processed)
    logger.info("  Flagged trades      : %d", total_flagged)
    logger.info("=" * 60)
    logger.info("Run `python main.py resolve` then `python main.py stats` for accuracy.")


if __name__ == "__main__":
    run()
