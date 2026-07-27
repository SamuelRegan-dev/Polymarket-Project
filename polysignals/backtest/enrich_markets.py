"""
Price-convergence heuristic for market winners — the weakest of the three
resolution sources, kept for comparison against the real ones.

Infers likely market winners from historical trade prices (last fill
>= HEURISTIC_WIN_THRESHOLD), not from Polymarket's actual resolution /
oracle data. Purely a local scan over ORDERFILLED_CSV_PATH; writes a
winning_outcome column back into MARKETS_CSV_PATH.

Ground truth lives in fetch_resolutions.py (on-chain payouts); a market
can trade at 0.92 and still resolve the other way, which is exactly the
gap this project measures.

NOT usable for the counter-test. Inferring the winner from late prices is
circular when the question under test is whether late trades predict
winners — compare.py excludes heuristic-only markets for that reason.
"""

import duckdb
import pandas as pd

from polysignals.config import (
    DEFAULT_COHORT,
    HEURISTIC_WIN_THRESHOLD,
    MARKETS_CSV_PATH,
    ORDERFILLED_CSV_PATH,
)
from polysignals.core import cohorts


def run(cohort: str = DEFAULT_COHORT) -> None:
    cohorts.keywords_for(cohort)  # fail fast on a typo'd cohort name

    print(f"Loading markets... [cohort={cohort}]")
    df = pd.read_csv(MARKETS_CSV_PATH, dtype=str, low_memory=False)
    if "winning_outcome" not in df.columns:
        df["winning_outcome"] = ""

    # Get all token IDs we actually need to look up
    in_cohort = df["question"].fillna("").apply(
        lambda q: cohorts.is_in_cohort(q, cohort)
    )
    cohort_rows = df[in_cohort]
    tokens = set(
        cohort_rows["token1"].dropna().tolist()
        + cohort_rows["token2"].dropna().tolist()
    )
    tokens = {t for t in tokens if str(t).strip() and str(t).strip().lower() != "nan"}

    if not tokens:
        print(f"No tokens found for cohort {cohort!r} — nothing to do.")
        return

    token_list = ", ".join(f"'{t}'" for t in tokens)

    print(f"Querying last prices for {len(tokens)} tokens from the trade CSV (may take 2-3 mins)...")
    con = duckdb.connect()

    last_price = con.execute(f"""
        SELECT token_id, price FROM (
            SELECT token_id, price,
                   ROW_NUMBER() OVER (PARTITION BY token_id ORDER BY timestamp DESC) AS rn
            FROM (
                -- direction 1: taker pays USDC, maker delivers tokens
                SELECT makerAssetId::VARCHAR AS token_id,
                       timestamp::BIGINT AS timestamp,
                       takerAmountFilled::DOUBLE / makerAmountFilled::DOUBLE AS price
                FROM read_csv_auto('{ORDERFILLED_CSV_PATH}', types={{'makerAssetId': 'VARCHAR', 'takerAssetId': 'VARCHAR'}})
                WHERE takerAssetId::VARCHAR = '0'
                  AND makerAssetId::VARCHAR IN ({token_list})
                  AND makerAmountFilled::DOUBLE > 0

                UNION ALL

                -- direction 2: maker pays USDC, taker delivers tokens
                SELECT takerAssetId::VARCHAR AS token_id,
                       timestamp::BIGINT AS timestamp,
                       makerAmountFilled::DOUBLE / takerAmountFilled::DOUBLE AS price
                FROM read_csv_auto('{ORDERFILLED_CSV_PATH}', types={{'makerAssetId': 'VARCHAR', 'takerAssetId': 'VARCHAR'}})
                WHERE makerAssetId::VARCHAR = '0'
                  AND takerAssetId::VARCHAR IN ({token_list})
                  AND takerAmountFilled::DOUBLE > 0
            )
        ) WHERE rn = 1
    """).df().set_index("token_id")["price"]

    con.close()
    print(f"Got prices for {len(last_price)} tokens")

    resolved = skipped = 0
    for idx, row in df.iterrows():
        existing = row.get("winning_outcome", "")
        if pd.notna(existing) and str(existing).strip():
            skipped += 1
            continue
        if not cohorts.is_in_cohort(row.get("question", ""), cohort):
            continue

        t1 = str(row.get("token1", "")).strip()
        t2 = str(row.get("token2", "")).strip()

        p1 = last_price.get(t1)
        p2 = last_price.get(t2)

        if p1 is not None and p1 >= HEURISTIC_WIN_THRESHOLD:
            df.at[idx, "winning_outcome"] = str(row.get("answer1", "Yes"))
            resolved += 1
        elif p2 is not None and p2 >= HEURISTIC_WIN_THRESHOLD:
            df.at[idx, "winning_outcome"] = str(row.get("answer2", "No"))
            resolved += 1

    df.to_csv(MARKETS_CSV_PATH, index=False)
    print(f"Done. Resolved: {resolved} | Already had winner: {skipped}")


if __name__ == "__main__":
    run()
