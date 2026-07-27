"""
DuckDB storage layer.

Three tables:
  markets          — markets from markets.csv (reference data)
  flagged_trades   — individual trades that hit a threshold
  market_summaries — per-market window statistics (even when nothing flagged)

Every table carries a `cohort` column identifying which market universe
the row belongs to (see core/cohorts.py). The counter-test compares
cohorts against each other, so they live in the same tables rather than
parallel ones: the comparison is then a GROUP BY instead of a UNION
across duplicated schemas that can drift apart. Two arms of a controlled
experiment silently diverging is the one bug that invalidates a result
without ever raising an error.

The database PERSISTS across runs. initialise() is idempotent — safe
to call on every run, since it only creates tables/columns that don't
already exist and never touches existing rows. Rows written before the
cohort column existed are backfilled to DEFAULT_COHORT rather than left
NULL, so an existing database keeps working without a reload.

reset() wipes everything; reset_signals() clears derived tables but keeps
resolutions; reset_cohort() clears a single arm. Nothing is called
automatically.
"""

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from polysignals.config import (
    DB_PATH,
    DEFAULT_COHORT,
    FLAT_THRESHOLD_USD,
    TOKEN_DECIMALS,
)
from polysignals.core.signals import FlaggedTrade

logger = logging.getLogger(__name__)

# markets.onchain_winner value recorded for the rare 50/50 UMA rulings —
# neither side won, so correctness comparisons treat it as NULL.
TIE = "50/50"

DDL = """
CREATE TABLE IF NOT EXISTS markets (
    market_id       VARCHAR PRIMARY KEY,
    question        VARCHAR,
    answer1         VARCHAR,
    answer2         VARCHAR,
    winning_outcome VARCHAR,
    volume          DOUBLE,
    closed_time     VARCHAR,
    condition_id    VARCHAR,
    token1          VARCHAR,
    token2          VARCHAR,
    market_slug     VARCHAR,
    neg_risk        BOOLEAN,
    onchain_winner  VARCHAR,
    cohort          VARCHAR
);

CREATE TABLE IF NOT EXISTS flagged_trades (
    recorded_at        TIMESTAMP,
    market_id          VARCHAR,
    market_question    VARCHAR,
    token_id           VARCHAR,
    outcome            VARCHAR,
    trade_timestamp    BIGINT,
    size               DOUBLE,
    price              DOUBLE,
    volume_usd         DOUBLE,
    hours_before_close DOUBLE,
    flat_threshold_hit BOOLEAN,
    pct_threshold_hit  BOOLEAN,
    amihud_ratio       DOUBLE,
    winning_outcome    VARCHAR,
    trade_was_correct  BOOLEAN,
    cohort             VARCHAR,
    UNIQUE (market_id, token_id, trade_timestamp)
);

CREATE TABLE IF NOT EXISTS market_summaries (
    recorded_at    TIMESTAMP,
    market_id      VARCHAR PRIMARY KEY,
    question       VARCHAR,
    trade_count    INTEGER,
    total_volume   DOUBLE,
    max_volume     DOUBLE,
    median_volume  DOUBLE,
    p90_volume     DOUBLE,
    flat_hits      INTEGER,
    cohort         VARCHAR
);
"""

# Column order for positional inserts. Declared explicitly rather than
# relying on VALUES (...) matching the DDL — ALTER TABLE appends columns
# in migration order, which need not match a freshly created table.
MARKET_COLUMNS = (
    "market_id", "question", "answer1", "answer2", "winning_outcome",
    "volume", "closed_time", "condition_id", "token1", "token2",
    "market_slug", "neg_risk", "onchain_winner", "cohort",
)

FLAGGED_COLUMNS = (
    "recorded_at", "market_id", "market_question", "token_id", "outcome",
    "trade_timestamp", "size", "price", "volume_usd", "hours_before_close",
    "flat_threshold_hit", "pct_threshold_hit", "amihud_ratio",
    "winning_outcome", "trade_was_correct", "cohort",
)

SUMMARY_COLUMNS = (
    "recorded_at", "market_id", "question", "trade_count", "total_volume",
    "max_volume", "median_volume", "p90_volume", "flat_hits", "cohort",
)


def _insert_sql(table: str, columns: tuple[str, ...]) -> str:
    placeholders = ", ".join("?" for _ in columns)
    return (
        f"INSERT OR IGNORE INTO {table} ({', '.join(columns)}) "
        f"VALUES ({placeholders})"
    )


@contextmanager
def _conn():
    conn = duckdb.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def reset():
    """
    Drop all tables and start clean.

    NOT called automatically — the database persists across runs (see
    initialise()). Call this yourself only if you deliberately want to
    wipe everything and rebuild from scratch.
    """
    with _conn() as conn:
        conn.execute("DROP TABLE IF EXISTS flagged_trades")
        conn.execute("DROP TABLE IF EXISTS market_summaries")
        conn.execute("DROP TABLE IF EXISTS markets")
    logger.info("Database reset — all tables dropped.")


def reset_signals():
    """
    Clear the derived signal tables (flagged_trades, market_summaries)
    but keep the markets table — including onchain_winner values already
    fetched. Use this to re-run the loader after changing signal logic:
    flagged_trades dedupes on re-insert, so stale rows would otherwise
    survive, and this avoids re-fetching on-chain resolutions.

    Note this clears EVERY cohort. To re-run one arm of the counter-test
    without touching the other, use reset_cohort().
    """
    with _conn() as conn:
        conn.execute("DROP TABLE IF EXISTS flagged_trades")
        conn.execute("DROP TABLE IF EXISTS market_summaries")
    logger.info("Signal tables cleared (all cohorts) — markets/onchain_winner kept.")


def reset_cohort(cohort: str, keep_markets: bool = True) -> None:
    """
    Clear one cohort's rows, leaving the other arm intact.

    Iterating on a keyword list is normal — the first sports list will not
    be the right one — and without this, every iteration means either
    dropping the committee data too and re-running the slow load, or
    leaving stale rows behind that INSERT OR IGNORE will not overwrite.

    keep_markets preserves the markets rows (and so the on-chain winners
    already fetched for them) while clearing derived signals. Pass False
    to drop the market rows as well, which is what you want if the
    keyword change alters *which* markets are in the cohort.
    """
    with _conn() as conn:
        conn.execute("DELETE FROM flagged_trades WHERE cohort = ?", [cohort])
        conn.execute("DELETE FROM market_summaries WHERE cohort = ?", [cohort])
        if not keep_markets:
            conn.execute("DELETE FROM markets WHERE cohort = ?", [cohort])
    scope = "signals" if keep_markets else "signals + markets"
    logger.info("Cleared %s for cohort %r.", scope, cohort)


def initialise():
    """
    Create tables/columns that don't exist yet. Safe to call on every
    run: CREATE TABLE IF NOT EXISTS and ADD COLUMN IF NOT EXISTS never
    touch existing rows, so re-running the loader never wipes prior
    data — including onchain_winner values fetch_resolutions.py wrote in.
    """
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.execute(DDL)
        # Defensive migrations for databases created before these columns.
        conn.execute("ALTER TABLE markets ADD COLUMN IF NOT EXISTS onchain_winner VARCHAR")
        for table in ("markets", "flagged_trades", "market_summaries"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS cohort VARCHAR")
            # Rows predating the cohort column came from the original
            # single-universe experiment. Backfilling rather than leaving
            # NULL means an existing database stays usable — and a slow
            # committee load does not have to be repeated just to add a
            # label that is already known.
            backfilled = conn.execute(
                f"UPDATE {table} SET cohort = ? WHERE cohort IS NULL",
                [DEFAULT_COHORT],
            ).fetchall()
            if backfilled and backfilled[0][0]:
                logger.info(
                    "Backfilled %d pre-cohort row(s) in %s to %r.",
                    backfilled[0][0], table, DEFAULT_COHORT,
                )
    logger.info("Storage initialised at %s", DB_PATH)


def repair_token_decimals(cutoff: str, dry_run: bool = False) -> dict:
    """
    Rescale rows written before the TOKEN_DECIMALS fix.

    Background: Polymarket outcome tokens use 6 decimals on-chain, so the
    raw makerAmountFilled/takerAmountFilled values in the OrderFilled CSV
    are integer base units. An earlier version of the loader treated them
    as whole shares, making size and volume_usd 1,000,000x too large —
    single "trades" priced at tens of billions of dollars. csv_loader.py
    divides by TOKEN_DECIMALS today; these are the rows from before that.

    Why an in-place repair is valid rather than a reload:
      - The old code flagged anything with inflated volume >= threshold,
        i.e. TRUE volume >= threshold/1e6. The correctly-flagged set is
        therefore a strict SUBSET of what is already stored — nothing that
        should be flagged is missing, so no CSV re-scan is needed.
      - pct_threshold_hit ranks trades within a market, so it is
        scale-invariant and already correct.
      - flat_threshold_hit is scale-dependent and is recomputed here.
      - amihud_ratio is |price change| / volume_usd, so it was 1e6 too
        SMALL and is corrected upward.

    Rows that no longer clear the flat threshold and were never a
    percentile hit are deleted — under correct units they were never
    signals at all.

    Idempotent: records itself in schema_migrations and refuses to run
    twice, because applying it a second time would divide correct data.
    """
    marker = f"token_decimals_rescale:{cutoff}"

    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name       VARCHAR PRIMARY KEY,
                applied_at TIMESTAMP
            )
        """)
        already = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", [marker]
        ).fetchall()
        if already:
            logger.info("Migration %r already applied — skipping.", marker)
            return {"skipped": True}

        before = conn.execute(
            """
            SELECT COUNT(*) AS affected,
                   COUNT(*) FILTER (WHERE volume_usd / ? >= ?)            AS keeps_flat,
                   COUNT(*) FILTER (WHERE volume_usd / ? <  ?
                                      AND NOT COALESCE(pct_threshold_hit, FALSE)) AS to_delete
            FROM flagged_trades
            WHERE recorded_at < ?
            """,
            [TOKEN_DECIMALS, FLAT_THRESHOLD_USD,
             TOKEN_DECIMALS, FLAT_THRESHOLD_USD, cutoff],
        ).fetchone()

        summaries = conn.execute(
            "SELECT COUNT(*) FROM market_summaries WHERE recorded_at < ?", [cutoff]
        ).fetchone()

        stats = {
            "skipped": False,
            "flagged_affected": int(before[0] or 0),
            "flagged_keeps_flat": int(before[1] or 0),
            "flagged_deleted": int(before[2] or 0),
            "summaries_affected": int(summaries[0] or 0),
        }

        if dry_run:
            logger.info("DRY RUN — no changes written. %s", stats)
            return stats

        # 1. Rescale the trade rows.
        conn.execute(
            """
            UPDATE flagged_trades
            SET size         = size / ?,
                volume_usd   = volume_usd / ?,
                amihud_ratio = amihud_ratio * ?
            WHERE recorded_at < ?
            """,
            [TOKEN_DECIMALS, TOKEN_DECIMALS, TOKEN_DECIMALS, cutoff],
        )

        # 2. Recompute the scale-dependent threshold. pct_threshold_hit is
        #    a within-market ranking and is deliberately left alone.
        conn.execute(
            """
            UPDATE flagged_trades
            SET flat_threshold_hit = (volume_usd >= ?)
            WHERE recorded_at < ?
            """,
            [FLAT_THRESHOLD_USD, cutoff],
        )

        # 3. Drop rows that were only ever "signals" because of the bug.
        conn.execute(
            """
            DELETE FROM flagged_trades
            WHERE recorded_at < ?
              AND NOT flat_threshold_hit
              AND NOT COALESCE(pct_threshold_hit, FALSE)
            """,
            [cutoff],
        )

        # 4. Rescale per-market volume statistics. These were computed over
        #    ALL window trades, not just flagged ones, so they cannot be
        #    rebuilt from flagged_trades — but they are pure volume figures,
        #    so dividing is exact.
        conn.execute(
            """
            UPDATE market_summaries
            SET total_volume  = total_volume  / ?,
                max_volume    = max_volume    / ?,
                median_volume = median_volume / ?,
                p90_volume    = p90_volume    / ?
            WHERE recorded_at < ?
            """,
            [TOKEN_DECIMALS] * 4 + [cutoff],
        )

        # 5. flat_hits counted window trades over the threshold. Post-repair
        #    the correct flat hits for a market are exactly its surviving
        #    flat-flagged rows, so this one IS recoverable.
        conn.execute(
            """
            UPDATE market_summaries AS ms
            SET flat_hits = COALESCE((
                SELECT COUNT(*) FROM flagged_trades ft
                WHERE ft.market_id = ms.market_id AND ft.flat_threshold_hit
            ), 0)
            WHERE ms.recorded_at < ?
            """,
            [cutoff],
        )

        conn.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            [marker, datetime.now(timezone.utc)],
        )

    logger.info("=" * 60)
    logger.info("TOKEN_DECIMALS REPAIR COMPLETE")
    logger.info("  Flagged rows rescaled : %d", stats["flagged_affected"])
    logger.info("  Still flat-threshold  : %d", stats["flagged_keeps_flat"])
    logger.info("  Deleted (not signals) : %d", stats["flagged_deleted"])
    logger.info("  Summaries rescaled    : %d", stats["summaries_affected"])
    logger.info("=" * 60)
    return stats


def insert_markets(markets_df, cohort: str = DEFAULT_COHORT) -> None:
    """
    Write markets from the enriched markets.csv DataFrame, tagged with the
    cohort they were selected for. Skips rows with no market id.
    Deduplicates on market_id — a market already in the table (e.g. from a
    previous run) is left untouched.
    """
    if markets_df is None or markets_df.empty:
        return

    def _safe(row, col, cast=str):
        val = row.get(col)
        if val is None or (isinstance(val, float) and str(val) == "nan"):
            return None
        try:
            return cast(val)
        except Exception:
            return None

    rows = []
    for _, row in markets_df.iterrows():
        market_id = _safe(row, "id") or _safe(row, "condition_id")
        if not market_id:
            continue
        rows.append((
            market_id,
            _safe(row, "question"),
            _safe(row, "answer1"),
            _safe(row, "answer2"),
            _safe(row, "winning_outcome"),
            _safe(row, "volume", float),
            _safe(row, "closedTime"),
            _safe(row, "condition_id"),
            _safe(row, "token1"),
            _safe(row, "token2"),
            _safe(row, "market_slug"),
            None,  # neg_risk — keep for schema completeness
            None,  # onchain_winner — populated later by fetch_resolutions.py
            cohort,
        ))

    if not rows:
        return

    with _conn() as conn:
        conn.executemany(_insert_sql("markets", MARKET_COLUMNS), rows)
    logger.info("Inserted %d market(s) into markets table [cohort=%s].", len(rows), cohort)


def insert_flagged_trades(trades: list[FlaggedTrade], cohort: str = DEFAULT_COHORT) -> None:
    if not trades:
        return

    now = datetime.now(timezone.utc)
    rows = [
        (
            now,
            ft.market_id,
            ft.market_question,
            ft.trade.token_id,
            ft.trade.outcome,
            ft.trade.timestamp,
            ft.trade.size,
            ft.trade.price,
            ft.trade.volume_usd,
            ft.hours_before_close,
            ft.flat_threshold_hit,
            ft.pct_threshold_hit,
            ft.amihud_ratio,
            ft.winning_outcome,
            ft.trade_was_correct,
            cohort,
        )
        for ft in trades
    ]

    with _conn() as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM flagged_trades WHERE cohort = ?", [cohort]
        ).fetchone()[0]
        conn.executemany(_insert_sql("flagged_trades", FLAGGED_COLUMNS), rows)
        after = conn.execute(
            "SELECT COUNT(*) FROM flagged_trades WHERE cohort = ?", [cohort]
        ).fetchone()[0]

    inserted = after - before
    skipped = len(rows) - inserted

    logger.info(
        "Inserted %d flagged trade(s) [cohort=%s] — %d of %d attempted; %d already present.",
        inserted, cohort, inserted, len(rows), skipped,
    )

    # A run that computes rows but writes none is nearly always stale data
    # winning: INSERT OR IGNORE dedupes on (market_id, token_id,
    # trade_timestamp), so re-loading after a LOGIC change silently keeps
    # the old values and discards the new ones. That is exactly how
    # pre-TOKEN_DECIMALS rows survived a re-load. Say so loudly.
    if rows and skipped:
        logger.warning(
            "%d row(s) were skipped as duplicates. If you re-ran this load "
            "after changing signal logic or units, those rows still hold the "
            "OLD values — `python main.py reset-cohort --cohort %s` first, or "
            "the recomputation is silently discarded.",
            skipped, cohort,
        )


def insert_market_summary(summary: dict, cohort: str = DEFAULT_COHORT) -> None:
    """
    Single-row insert. Kept for callers that handle one market at a time
    (the live scanner); the CSV loader uses the batched
    insert_market_summaries() below so it isn't opening a fresh
    connection per market.
    """
    if not summary or not summary.get("market_id"):
        return

    now = datetime.now(timezone.utc)
    with _conn() as conn:
        conn.execute(
            _insert_sql("market_summaries", SUMMARY_COLUMNS),
            (
                now,
                summary.get("market_id"),
                summary.get("question"),
                summary.get("trade_count", 0),
                summary.get("total_volume", 0.0),
                summary.get("max_volume", 0.0),
                summary.get("median_volume", 0.0),
                summary.get("p90_volume", 0.0),
                summary.get("flat_hits", 0),
                cohort,
            ),
        )


def insert_market_summaries(summaries: list[dict], cohort: str = DEFAULT_COHORT) -> None:
    """Batched version of insert_market_summary — one connection for the
    whole list instead of one per market."""
    if not summaries:
        return

    now = datetime.now(timezone.utc)
    rows = [
        (
            now,
            s.get("market_id"),
            s.get("question"),
            s.get("trade_count", 0),
            s.get("total_volume", 0.0),
            s.get("max_volume", 0.0),
            s.get("median_volume", 0.0),
            s.get("p90_volume", 0.0),
            s.get("flat_hits", 0),
            cohort,
        )
        for s in summaries
        if s.get("market_id")
    ]
    if not rows:
        return

    with _conn() as conn:
        conn.executemany(_insert_sql("market_summaries", SUMMARY_COLUMNS), rows)
    logger.info("Inserted %d market summary row(s) [cohort=%s].", len(rows), cohort)


def get_markets_needing_resolution(cohort: str | None = None) -> list[dict]:
    """
    Markets that have a condition_id but no on-chain resolution recorded
    yet. Used by fetch_resolutions.py instead of the source CSV.

    Cohort-agnostic by default: on-chain resolution is ground truth and
    costs the same RPC call regardless of which arm the market is in, so
    resolving everything in one pass is both simpler and cheaper than
    running it per cohort. Pass a cohort to narrow it anyway.
    """
    sql = """
        SELECT market_id, question, answer1, answer2, condition_id, cohort
        FROM   markets
        WHERE  condition_id IS NOT NULL
          AND  (onchain_winner IS NULL OR onchain_winner = '')
    """
    params: list = []
    if cohort is not None:
        sql += " AND cohort = ?"
        params.append(cohort)

    with _conn() as conn:
        result = conn.execute(sql, params).fetchdf()
    return result.to_dict("records")


def update_onchain_winner(market_id: str, onchain_winner: str) -> None:
    """Write a single resolved outcome back to the markets table."""
    with _conn() as conn:
        conn.execute(
            "UPDATE markets SET onchain_winner = ? WHERE market_id = ?",
            [onchain_winner, market_id],
        )


_COHORT_COUNTS_SQL = f"""
    SELECT
        m.cohort,
        COUNT(DISTINCT m.market_id)                      AS markets,
        COUNT(DISTINCT m.market_id) FILTER (
            WHERE NULLIF(m.onchain_winner, '') IS NOT NULL
              AND m.onchain_winner != '{TIE}'
        )                                               AS onchain_resolved,
        (SELECT COUNT(*) FROM flagged_trades f
          WHERE f.cohort = m.cohort)                     AS flagged_trades
    FROM   markets m
    GROUP  BY m.cohort
    ORDER  BY m.cohort
"""


def cohort_counts(conn=None) -> list[dict]:
    """
    Row counts per cohort across all three tables — a quick check that
    both arms actually loaded before running any comparison.

    Accepts an existing connection. DuckDB refuses a second connection to
    the same file when the configuration differs, so a read-only caller
    (analysis, compare) must pass its own connection rather than let this
    open a read-write one behind its back.
    """
    if conn is not None:
        return conn.execute(_COHORT_COUNTS_SQL).fetchdf().to_dict("records")

    with _conn() as owned:
        return owned.execute(_COHORT_COUNTS_SQL).fetchdf().to_dict("records")


def query_flagged(min_volume: float = 0.0, cohort: str | None = None) -> list[dict]:
    """
    Quick read helper for inspection / notebooks.

    trade_was_correct_live is recomputed against markets.onchain_winner
    (falling back to the CSV-heuristic markets.winning_outcome if no
    on-chain result yet) — NOT flagged_trades.trade_was_correct, which
    is frozen at load time and never updated. 50/50 on-chain rulings
    yield NULL: neither side was "correct".
    """
    sql = """
        SELECT
            ft.*,
            m.onchain_winner,
            m.winning_outcome AS market_winning_outcome,
            CASE
                WHEN NULLIF(m.onchain_winner, '') = ?
                    THEN NULL
                WHEN COALESCE(NULLIF(m.onchain_winner, ''), NULLIF(m.winning_outcome, '')) IS NULL
                    THEN NULL
                ELSE COALESCE(NULLIF(m.onchain_winner, ''), NULLIF(m.winning_outcome, '')) = ft.outcome
            END AS trade_was_correct_live
        FROM      flagged_trades ft
        LEFT JOIN markets m ON ft.market_id = m.market_id
        WHERE     ft.volume_usd >= ?
    """
    params: list = [TIE, min_volume]
    if cohort is not None:
        sql += " AND ft.cohort = ?"
        params.append(cohort)
    sql += " ORDER BY ft.trade_timestamp DESC"

    with _conn() as conn:
        result = conn.execute(sql, params).fetchdf()
    return result.to_dict("records")
