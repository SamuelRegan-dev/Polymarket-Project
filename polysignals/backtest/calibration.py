"""
Calibration / edge analysis — is the signal actually worth anything, or
does it just confirm markets that were already lopsided?

A flagged trade being "correct" is close to meaningless on its own: if the
market was already at 0.92 for the eventual winner, calling it correct is
nearly automatic. What matters is whether the market's implied probability
(the trade's own execution price) was *accurate* — i.e. calibration.

For trades executed around price p, a well-calibrated market resolves that
side roughly p% of the time. If the actual win rate in a price bucket is
higher than the average price in that bucket, the market was underpricing
that side — genuine, monetizable edge. If actual win rate matches price,
following the flagged trade is worth nothing beyond the fair odds already
on offer, however often it's technically "correct".

edge = win_rate - avg_price
     = expected profit per $1 staked buying the flagged side at that price
       (payout $1 on a win, $0 on a loss; cost = price either way)

Uses the same on-chain/heuristic resolution precedence as analysis.py
(imports its resolved_cte() builder rather than duplicating it), and is
cohort-scoped the same way.

Note: price is a ratio of two same-scaled raw amounts, so it is NOT
affected by the token-decimals unit bug that inflated size/volume_usd —
this analysis is valid on the database as-is, independent of whether
csv_loader has been re-run since that fix.
"""

import duckdb

from polysignals.backtest.analysis import _bucket_case, _price_label, resolved_cte
from polysignals.config import DB_PATH, PRICE_BUCKETS

BUCKET_CASE = _bucket_case("price", PRICE_BUCKETS, _price_label)


def run(cohort: str | None = None, require_onchain: bool = False) -> None:
    conn = duckdb.connect(DB_PATH, read_only=True)
    cte = resolved_cte(cohort, require_onchain)

    scope = cohort if cohort else "ALL COHORTS (pooled)"
    print(f"\n{'#' * 66}")
    print(f"#  CALIBRATION — cohort: {scope}")
    if require_onchain:
        print("#  restricted to on-chain resolutions")
    print(f"{'#' * 66}")

    print("\n=== 1. CALIBRATION — TRADE-LEVEL (every flagged trade) ===")
    print(
        "    Is the market's own price, at the moment of the flagged trade,\n"
        "    an accurate probability? win_rate ≈ avg_price means no edge —\n"
        "    the trade just confirmed what the market already priced in.\n"
    )
    print(conn.execute(f"""
        {cte}
        SELECT
            bucket,
            COUNT(*)                                                  AS n,
            ROUND(AVG(price), 3)                                      AS avg_price,
            ROUND(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END), 3)     AS win_rate,
            ROUND(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END)
                  - AVG(price), 3)                                    AS edge
        FROM (SELECT *, {BUCKET_CASE} FROM resolved WHERE correct IS NOT NULL)
        GROUP BY bucket
        ORDER BY MIN(price)
    """).df().to_string(index=False))

    print("\n=== 2. CALIBRATION — MARKET-LEVEL (largest flagged trade per market) ===")
    print(
        "    Same idea, one vote per market — the honest sample size.\n"
    )
    print(conn.execute(f"""
        {cte}
        SELECT
            bucket,
            COUNT(*)                                                  AS markets,
            ROUND(AVG(price), 3)                                      AS avg_price,
            ROUND(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END), 3)     AS win_rate,
            ROUND(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END)
                  - AVG(price), 3)                                    AS edge
        FROM (
            SELECT *, {BUCKET_CASE},
                   ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY volume_usd DESC) AS rn
            FROM resolved
            WHERE correct IS NOT NULL
        )
        WHERE rn = 1
        GROUP BY bucket
        ORDER BY MIN(price)
    """).df().to_string(index=False))

    print("\n=== 3. OVERALL EDGE (market-level, all buckets combined) ===")
    print(conn.execute(f"""
        {cte}
        SELECT
            COUNT(*)                                                  AS markets,
            ROUND(AVG(price), 3)                                      AS avg_price,
            ROUND(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END), 3)     AS win_rate,
            ROUND(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END)
                  - AVG(price), 3)                                    AS edge_per_dollar_staked
        FROM (
            SELECT *, ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY volume_usd DESC) AS rn
            FROM resolved
            WHERE correct IS NOT NULL
        )
        WHERE rn = 1
    """).df().to_string(index=False))

    print(
        "\nRead this as: mirroring the single largest flagged trade in every\n"
        "market, at its own execution price, this is the average profit per\n"
        "$1 staked. Compare against the bucket table above — a positive\n"
        "overall edge driven entirely by the top price bucket is a rounding\n"
        "artifact of near-certain markets, not a strategy; edge concentrated\n"
        "in the mid-range buckets is the real thing."
    )

    conn.close()


if __name__ == "__main__":
    run()
