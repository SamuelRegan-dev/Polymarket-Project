"""
Backtest analysis — run after the pipeline has populated the database.

Read-only queries against DuckDB answering the core question: does the
signal exist? A signal "exists" if flagged trades predict the correct
outcome at a rate meaningfully above 50% (random chance), with
p < SIGNIFICANCE_ALPHA.

Correctness is computed live against the best available resolution for
each market: the on-chain payout (markets.onchain_winner — ground truth)
when present, falling back to the price-convergence heuristic
(markets.winning_outcome). 50/50 UMA rulings are excluded — neither side
"won". The frozen flagged_trades.trade_was_correct column is deliberately
ignored.

Two accuracy levels are reported:
  - trade-level  : every flagged trade counts. Inflated by clustering —
                   one market can contribute dozens of correlated flags.
  - market-level : one vote per market (its single largest flagged
                   trade). Statistically honest; use this number.

Everything here is cohort-scoped. Run with a cohort to analyse one arm;
run without to pool them, which is almost never what you want — see
compare.py for the arm-vs-arm counter-test.

Bucket boundaries come from config (TIME_BUCKETS_HOURS, SIZE_BUCKETS_USD,
PRICE_BUCKETS). Where the edge concentrates is itself a finding, so the
boundaries are configuration, not literals buried in SQL.
"""

import math

import duckdb

from polysignals.config import (
    DB_PATH,
    MIN_RESOLVED_SAMPLE,
    SIGNIFICANCE_ALPHA,
    SIZE_BUCKETS_USD,
    TIME_BUCKETS_HOURS,
)

TIE_VALUE = "50/50"


# ── Bucket SQL builders ───────────────────────────────────────────────────────

def _bucket_case(column: str, bounds: list[float], label_fn, alias: str = "bucket") -> str:
    """
    Build a CASE expression bucketing `column` by ascending upper bounds.

    label_fn(lower, upper) renders each label; upper is None for the final
    open-ended bucket. Ordering is handled by the callers with
    ORDER BY MIN(column), so labels don't need a sort prefix.
    """
    if not bounds:
        return f"'all' AS {alias}"

    clauses = []
    lower = None
    for upper in bounds:
        clauses.append(f"WHEN {column} < {upper} THEN '{label_fn(lower, upper)}'")
        lower = upper
    return (
        "CASE\n        "
        + "\n        ".join(clauses)
        + f"\n        ELSE '{label_fn(lower, None)}'\n    END AS {alias}"
    )


def _fmt_usd(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:g}M"
    if value >= 1_000:
        return f"${value / 1_000:g}k"
    return f"${value:g}"


def _time_label(lower, upper) -> str:
    if lower is None:
        return f"0-{upper:g}h"
    if upper is None:
        return f"{lower:g}h+"
    return f"{lower:g}-{upper:g}h"


def _size_label(lower, upper) -> str:
    if lower is None:
        return f"<{_fmt_usd(upper)}"
    if upper is None:
        return f"{_fmt_usd(lower)}+"
    return f"{_fmt_usd(lower)}-{_fmt_usd(upper)}"


def _price_label(lower, upper) -> str:
    if lower is None:
        return f"<{upper:.2f}"
    if upper is None:
        return f"{lower:.2f}+"
    return f"{lower:.2f}-{upper:.2f}"


TIME_BUCKET_CASE = _bucket_case("hours_before_close", TIME_BUCKETS_HOURS, _time_label)
SIZE_BUCKET_CASE = _bucket_case("volume_usd", SIZE_BUCKETS_USD, _size_label)


# ── Resolution CTE ────────────────────────────────────────────────────────────

def resolved_cte(cohort: str | None = None, require_onchain: bool = False) -> str:
    """
    CTE joining each flagged trade to its market's best resolution.

    winner precedence: on-chain > heuristic; '50/50' on-chain ties → NULL.

    require_onchain drops heuristic-resolved markets entirely. The
    heuristic infers the winner from late price convergence, which is
    partly circular when the question under test is whether late trades
    predict outcomes — see compare.py, where it is always on.
    """
    filters = []
    if cohort is not None:
        # Cohort lives on both tables; filter the trade side, which is what
        # every downstream aggregate counts.
        filters.append(f"ft.cohort = '{cohort}'")
    if require_onchain:
        filters.append(
            f"NULLIF(m.onchain_winner, '') IS NOT NULL "
            f"AND m.onchain_winner != '{TIE_VALUE}'"
        )
    where = f"WHERE {' AND '.join(filters)}" if filters else ""

    return f"""
WITH resolved AS (
    SELECT
        ft.*,
        COALESCE(NULLIF(m.onchain_winner, ''), NULLIF(m.winning_outcome, '')) AS winner,
        CASE
            WHEN NULLIF(m.onchain_winner, '')  IS NOT NULL THEN 'onchain'
            WHEN NULLIF(m.winning_outcome, '') IS NOT NULL THEN 'heuristic'
        END AS resolution_source,
        CASE
            WHEN NULLIF(m.onchain_winner, '') = '{TIE_VALUE}' THEN NULL
            WHEN COALESCE(NULLIF(m.onchain_winner, ''), NULLIF(m.winning_outcome, '')) IS NULL THEN NULL
            ELSE COALESCE(NULLIF(m.onchain_winner, ''), NULLIF(m.winning_outcome, '')) = ft.outcome
        END AS correct
    FROM flagged_trades ft
    LEFT JOIN markets m USING (market_id)
    {where}
)
"""


# Backwards-compatible default (all cohorts, any resolution source).
RESOLVED = resolved_cte()


# ── Statistics ────────────────────────────────────────────────────────────────

def binomial_test(correct: int, total: int) -> tuple[float, float]:
    """Two-sided z-test against p=0.5. Returns (z, p_value).
    Normal approximation — fine at these sample sizes."""
    if not total:
        return 0.0, 1.0
    p_hat = correct / total
    z = (p_hat - 0.5) / math.sqrt(0.25 / total)
    p_value = math.erfc(abs(z) / math.sqrt(2))
    return z, p_value


# Kept as a private alias: existing callers imported the underscored name.
_binomial_test = binomial_test


def _print_significance(label: str, correct: int, total: int) -> None:
    if not total:
        print(f"  {label}: no resolved observations")
        return
    z, p = binomial_test(correct, total)
    verdict = (
        f"significant at p<{SIGNIFICANCE_ALPHA}"
        if p < SIGNIFICANCE_ALPHA
        else "NOT significant"
    )
    warn = "  [UNDERPOWERED]" if total < MIN_RESOLVED_SAMPLE else ""
    print(
        f"  {label}: {correct}/{total} correct "
        f"({correct / total * 100:.1f}%) | z={z:+.2f} | p={p:.4f} → {verdict}{warn}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run(cohort: str | None = None, require_onchain: bool = False):
    conn = duckdb.connect(DB_PATH, read_only=True)
    cte = resolved_cte(cohort, require_onchain)

    scope = cohort if cohort else "ALL COHORTS (pooled)"
    print(f"\n{'#' * 66}")
    print(f"#  ANALYSIS — cohort: {scope}")
    if require_onchain:
        print("#  restricted to on-chain resolutions")
    print(f"{'#' * 66}")

    if cohort is None:
        print(
            "\n  NOTE: pooling cohorts mixes the treatment and control arms.\n"
            "  Pass a cohort, or use `main.py compare` for the counter-test."
        )

    # ── 0. Resolution coverage — how much of the sample is ground truth? ─────
    print("\n=== 0. RESOLUTION COVERAGE (markets table) ===")
    coverage_where = "WHERE cohort = ?" if cohort else ""
    coverage_params = [cohort] if cohort else []
    print(conn.execute(f"""
        SELECT
            cohort,
            COUNT(*)                                                        AS markets,
            COUNT(*) FILTER (WHERE NULLIF(onchain_winner, '') IS NOT NULL
                               AND onchain_winner != '{TIE_VALUE}')         AS onchain_resolved,
            COUNT(*) FILTER (WHERE onchain_winner = '{TIE_VALUE}')          AS onchain_5050,
            COUNT(*) FILTER (WHERE NULLIF(onchain_winner, '') IS NULL
                               AND NULLIF(winning_outcome, '') IS NOT NULL) AS heuristic_only,
            COUNT(*) FILTER (WHERE NULLIF(onchain_winner, '') IS NULL
                               AND NULLIF(winning_outcome, '') IS NULL)     AS unresolved
        FROM markets
        {coverage_where}
        GROUP BY cohort
        ORDER BY cohort
    """, coverage_params).df().to_string(index=False))

    print("\n    Heuristic vs on-chain agreement (markets with both):")
    print(conn.execute(f"""
        SELECT
            cohort,
            COUNT(*)                                                 AS both_present,
            COUNT(*) FILTER (WHERE winning_outcome = onchain_winner) AS agree,
            ROUND(100.0 * COUNT(*) FILTER (WHERE winning_outcome = onchain_winner)
                  / NULLIF(COUNT(*), 0), 1)                          AS agree_pct
        FROM markets
        WHERE NULLIF(onchain_winner, '')  IS NOT NULL
          AND onchain_winner != '{TIE_VALUE}'
          AND NULLIF(winning_outcome, '') IS NOT NULL
          {"AND cohort = ?" if cohort else ""}
        GROUP BY cohort
        ORDER BY cohort
    """, coverage_params).df().to_string(index=False))

    # ── 1. Top-line hit rate, trade-level ────────────────────────────────────
    print("\n=== 1. HIT RATE BY THRESHOLD — TRADE-LEVEL ===")
    topline = conn.execute(cte + """
        SELECT
            'flat'                                            AS threshold,
            COUNT(*)                                          AS total_flags,
            COUNT(*) FILTER (WHERE correct)                   AS correct,
            COUNT(*) FILTER (WHERE NOT correct)               AS wrong,
            COUNT(*) FILTER (WHERE correct IS NULL)           AS unresolved,
            ROUND(100.0 * COUNT(*) FILTER (WHERE correct)
                / NULLIF(COUNT(*) FILTER (WHERE correct IS NOT NULL), 0), 1) AS hit_rate_pct
        FROM resolved WHERE flat_threshold_hit
        UNION ALL
        SELECT
            'pct', COUNT(*),
            COUNT(*) FILTER (WHERE correct),
            COUNT(*) FILTER (WHERE NOT correct),
            COUNT(*) FILTER (WHERE correct IS NULL),
            ROUND(100.0 * COUNT(*) FILTER (WHERE correct)
                / NULLIF(COUNT(*) FILTER (WHERE correct IS NOT NULL), 0), 1)
        FROM resolved WHERE pct_threshold_hit
    """).df()
    print(topline.to_string(index=False))

    # Same, restricted to on-chain resolutions only (the trustworthy subset)
    print("\n    On-chain resolutions only:")
    print(conn.execute(cte + """
        SELECT
            COUNT(*) FILTER (WHERE correct IS NOT NULL)       AS resolved_flags,
            COUNT(*) FILTER (WHERE correct)                   AS correct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE correct)
                / NULLIF(COUNT(*) FILTER (WHERE correct IS NOT NULL), 0), 1) AS hit_rate_pct
        FROM resolved
        WHERE flat_threshold_hit AND resolution_source = 'onchain'
    """).df().to_string(index=False))

    # ── 2. Market-level: one vote per market (largest flagged trade) ────────
    print("\n=== 2. HIT RATE — MARKET-LEVEL (one vote per market) ===")
    market_level = conn.execute(cte + """
        SELECT
            COUNT(*)                        AS markets,
            COUNT(*) FILTER (WHERE correct) AS correct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE correct) / NULLIF(COUNT(*), 0), 1) AS hit_rate_pct
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY market_id ORDER BY volume_usd DESC
            ) AS rn
            FROM resolved
            WHERE correct IS NOT NULL AND flat_threshold_hit
        )
        WHERE rn = 1
    """).df()
    print(market_level.to_string(index=False))

    # ── 3. Statistical significance ──────────────────────────────────────────
    print("\n=== 3. SIGNIFICANCE vs. COIN FLIP (two-sided z-test) ===")
    flat_row = topline[topline["threshold"] == "flat"].iloc[0]
    _print_significance(
        "Trade-level (flat) ",
        int(flat_row["correct"]),
        int(flat_row["correct"]) + int(flat_row["wrong"]),
    )
    if not market_level.empty:
        _print_significance(
            "Market-level (flat)",
            int(market_level.iloc[0]["correct"]),
            int(market_level.iloc[0]["markets"]),
        )
    print("  NOTE: trade-level flags cluster within markets and are not\n"
          "  independent — treat the market-level test as the honest one.")

    # ── 4. Hit rate by hours before close ────────────────────────────────────
    print("\n=== 4. HIT RATE BY TIME BEFORE CLOSE (flat threshold) ===")
    print(conn.execute(cte + f"""
        SELECT
            {TIME_BUCKET_CASE.replace(' AS bucket', ' AS time_bucket')},
            COUNT(*)                                          AS flags,
            COUNT(*) FILTER (WHERE correct)                   AS correct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE correct)
                / NULLIF(COUNT(*), 0), 1)                     AS hit_rate_pct
        FROM resolved
        WHERE flat_threshold_hit AND correct IS NOT NULL
        GROUP BY 1
        ORDER BY MIN(hours_before_close)
    """).df().to_string(index=False))

    # ── 5. Hit rate by trade size bucket ─────────────────────────────────────
    print("\n=== 5. HIT RATE BY TRADE SIZE (flat threshold) ===")
    print(conn.execute(cte + f"""
        SELECT
            {SIZE_BUCKET_CASE.replace(' AS bucket', ' AS size_bucket')},
            COUNT(*)                                          AS flags,
            COUNT(*) FILTER (WHERE correct)                   AS correct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE correct)
                / NULLIF(COUNT(*), 0), 1)                     AS hit_rate_pct
        FROM resolved
        WHERE flat_threshold_hit AND correct IS NOT NULL
        GROUP BY 1
        ORDER BY MIN(volume_usd)
    """).df().to_string(index=False))

    # ── 6. Per-market summary ─────────────────────────────────────────────────
    print("\n=== 6. PER-MARKET FLAG SUMMARY (top 20 by flags) ===")
    print(conn.execute(cte + """
        SELECT
            market_question,
            resolution_source,
            COUNT(*)                                          AS flags,
            COUNT(*) FILTER (WHERE correct)                   AS correct,
            ROUND(AVG(volume_usd), 0)                         AS avg_vol_usd,
            ROUND(MIN(hours_before_close), 1)                 AS earliest_flag_h
        FROM resolved
        WHERE correct IS NOT NULL
        GROUP BY market_question, resolution_source
        ORDER BY flags DESC
        LIMIT 20
    """).df().to_string(index=False))

    # ── 7. Raw data sample ───────────────────────────────────────────────────
    print("\n=== 7. RAW FLAGGED TRADES (most recent 10) ===")
    print(conn.execute(cte + """
        SELECT
            market_question,
            outcome,
            winner,
            resolution_source,
            correct,
            ROUND(volume_usd, 0)         AS volume_usd,
            ROUND(hours_before_close, 1) AS hours_before_close,
            flat_threshold_hit,
            pct_threshold_hit
        FROM resolved
        ORDER BY trade_timestamp DESC
        LIMIT 10
    """).df().to_string(index=False))

    conn.close()


if __name__ == "__main__":
    run()
