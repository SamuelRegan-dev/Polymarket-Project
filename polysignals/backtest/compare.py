"""
The counter-test: does the signal survive a control universe?

The original experiment found that large, anomalous trades placed shortly
before a committee-decided market closes tend to be on the winning side.
Two explanations fit that observation equally well:

  H1 (the claim)  Committee members and their circles trade on private
                  knowledge of the outcome. The detector finds insider
                  activity.
  H0 (the worry)  Large trades near ANY market's resolution tend to be
                  correct, because by then the outcome is partly knowable
                  from public information and informed money is simply
                  more active. The detector finds market microstructure.

The two are indistinguishable from the committee arm alone. This module
distinguishes them by running the identical detector over a control
universe where H1 is impossible by construction — sports markets, where
nobody knows the final score in advance — and comparing.

  similar performance in both arms  → H0. The signal is not about insiders.
  materially better in committee    → H1 survives this test.

Three methodological commitments, each of which changes the answer:

  1. ON-CHAIN RESOLUTIONS ONLY (COMPARISON_REQUIRE_ONCHAIN).
     The price-convergence heuristic infers a market's winner FROM late
     prices. Using it here would be circular — the hypothesis is about
     whether late trades predict outcomes — and the circularity is worse
     in the more liquid control arm, which would manufacture a false null.

  2. MARKET-LEVEL, NOT TRADE-LEVEL.
     One market can contribute dozens of correlated flags. Counting them
     independently inflates every sample size and every significance test.
     One vote per market (its largest flagged trade) is the honest unit.

  3. EQUIVALENCE, NOT JUST NON-SIGNIFICANCE.
     "The arms are not significantly different" is not evidence they are
     the same — with small samples, nothing is significantly different
     from anything. The verdict below only concludes H0 when the
     confidence interval on the difference actually FITS INSIDE the
     equivalence margin. Otherwise it reports the test as underpowered,
     which is a different and more honest answer than "signal is bunk".

Read the verdict last. The parity and contamination diagnostics above it
determine whether the comparison means anything in the first place.
"""

from __future__ import annotations

import math

import duckdb
import pandas as pd

from polysignals.backtest.analysis import (
    TIME_BUCKET_CASE,
    binomial_test,
    resolved_cte,
)
from polysignals.config import (
    COMPARISON_REQUIRE_ONCHAIN,
    DB_PATH,
    EQUIVALENCE_MARGIN,
    MIN_RESOLVED_SAMPLE,
    POST_OUTCOME_PRICE_FLOOR,
    SIGNIFICANCE_ALPHA,
)
from polysignals.core import cohorts, storage


# ── Statistics ────────────────────────────────────────────────────────────────

def z_critical(alpha: float) -> float:
    """
    Two-sided critical z for the given alpha, by bisection on erfc.

    Avoids a scipy dependency for one number. erfc(z/sqrt2) is the
    two-sided tail probability and is monotonically decreasing in z, so
    bisection converges cleanly.
    """
    lo, hi = 0.0, 10.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if math.erfc(mid / math.sqrt(2)) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def two_proportion_test(
    correct_a: int, total_a: int,
    correct_b: int, total_b: int,
) -> dict:
    """
    Two-sided z-test for a difference between two independent proportions,
    plus a confidence interval on that difference.

    The test statistic uses the POOLED standard error (correct under the
    null that both proportions are equal). The interval uses the UNPOOLED
    standard error, which does not assume they are equal — the standard
    pairing, and the interval is what the equivalence verdict reads.
    """
    if not total_a or not total_b:
        return {
            "p_a": None, "p_b": None, "diff": None,
            "z": 0.0, "p_value": 1.0, "ci_low": None, "ci_high": None,
            "usable": False,
        }

    p_a = correct_a / total_a
    p_b = correct_b / total_b
    diff = p_a - p_b

    pooled = (correct_a + correct_b) / (total_a + total_b)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / total_a + 1 / total_b))
    z = diff / se_pooled if se_pooled > 0 else 0.0
    p_value = math.erfc(abs(z) / math.sqrt(2))

    se_unpooled = math.sqrt(
        p_a * (1 - p_a) / total_a + p_b * (1 - p_b) / total_b
    )
    crit = z_critical(SIGNIFICANCE_ALPHA)

    return {
        "p_a": p_a,
        "p_b": p_b,
        "diff": diff,
        "z": z,
        "p_value": p_value,
        "ci_low": diff - crit * se_unpooled,
        "ci_high": diff + crit * se_unpooled,
        "usable": True,
    }


def minimum_detectable_effect(total_a: int, total_b: int) -> float | None:
    """
    Roughly the smallest difference in hit rate this sample could detect
    at the configured alpha, assuming both arms sit near 50%.

    Reported so an inconclusive result can be read properly: if the MDE is
    20 percentage points, "no significant difference" rules out almost
    nothing.
    """
    if not total_a or not total_b:
        return None
    se = math.sqrt(0.25 / total_a + 0.25 / total_b)
    return z_critical(SIGNIFICANCE_ALPHA) * se


# ── Queries ───────────────────────────────────────────────────────────────────

def _market_level_counts(conn, cohort: str, require_onchain: bool) -> tuple[int, int]:
    """(markets, correct) using one vote per market — its largest flagged trade."""
    row = conn.execute(resolved_cte(cohort, require_onchain) + """
        SELECT
            COUNT(*)                        AS markets,
            COUNT(*) FILTER (WHERE correct) AS correct
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY market_id ORDER BY volume_usd DESC
            ) AS rn
            FROM resolved
            WHERE correct IS NOT NULL AND flat_threshold_hit
        )
        WHERE rn = 1
    """).fetchone()
    return (int(row[0] or 0), int(row[1] or 0))


def _time_edge_table(conn, cohort: str, require_onchain: bool) -> pd.DataFrame:
    """
    Market-level hit rate AND edge, split by how long before close the
    market's largest flagged trade landed.

    This crosstab is the actual deliverable: analysis.py buckets hit rate
    by time but never prices it, and calibration.py prices edge but buckets
    by price, not time. Neither answers "in the time band where the edge is
    real, do the two cohorts differ?" — which is the comparison that
    matters, since a signal that only works far from close is a different
    claim than one that only works in the final hour.
    """
    return conn.execute(resolved_cte(cohort, require_onchain) + f"""
        SELECT
            {TIME_BUCKET_CASE.replace(' AS bucket', ' AS time_bucket')},
            COUNT(*)                                               AS markets,
            COUNT(*) FILTER (WHERE correct)                        AS correct,
            ROUND(100.0 * COUNT(*) FILTER (WHERE correct)
                  / NULLIF(COUNT(*), 0), 1)                        AS hit_rate_pct,
            ROUND(AVG(price), 3)                                   AS avg_price,
            ROUND(AVG(CASE WHEN correct THEN 1.0 ELSE 0.0 END)
                  - AVG(price), 3)                                 AS edge,
            MIN(hours_before_close)                                AS _sort
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY market_id ORDER BY volume_usd DESC
            ) AS rn
            FROM resolved
            WHERE correct IS NOT NULL AND flat_threshold_hit
        )
        WHERE rn = 1
        GROUP BY 1
        ORDER BY MIN(hours_before_close)
    """).df()


def _threshold_parity(conn, cohort: str) -> dict:
    """
    Is the flat dollar threshold comparable across cohorts?

    FLAT_THRESHOLD_USD is an absolute figure. In an illiquid awards market
    it selects the top fraction of a percent of trades; in a liquid sports
    market the same dollar amount may be unremarkable. If the flag rates
    diverge badly, the two arms are not running the same experiment and
    the percentile threshold is the fairer comparator.
    """
    row = conn.execute("""
        SELECT
            (SELECT COUNT(*) FROM flagged_trades WHERE cohort = ?)          AS flagged,
            (SELECT COALESCE(SUM(trade_count), 0) FROM market_summaries
              WHERE cohort = ?)                                             AS window_trades,
            (SELECT COALESCE(MEDIAN(volume_usd), 0) FROM flagged_trades
              WHERE cohort = ?)                                             AS median_flag_usd,
            (SELECT COUNT(*) FROM markets WHERE cohort = ?)                 AS markets
    """, [cohort, cohort, cohort, cohort]).fetchone()

    flagged, window_trades, median_flag, markets = row
    return {
        "cohort": cohort,
        "markets": int(markets or 0),
        "window_trades": int(window_trades or 0),
        "flagged": int(flagged or 0),
        "flag_rate_pct": (100.0 * flagged / window_trades) if window_trades else None,
        "median_flag_usd": float(median_flag or 0),
    }


def _contamination(conn, cohort: str, require_onchain: bool) -> dict:
    """
    What share of flagged trades executed at a price so high the outcome
    was probably already public?

    The specific worry for the sports arm: if a market's recorded close
    time lags the end of the actual event, the signal window swallows
    trading that happened when the result was already known. That produces
    a near-perfect hit rate that has nothing to do with prediction — and
    would make the control arm look like it matches the treatment arm for
    an entirely artifactual reason.

    A high share here in sports but not committee means the comparison is
    contaminated and the close-time alignment needs checking before the
    verdict means anything.
    """
    row = conn.execute(resolved_cte(cohort, require_onchain) + f"""
        SELECT
            COUNT(*)                                                     AS n,
            COUNT(*) FILTER (WHERE price >= {POST_OUTCOME_PRICE_FLOOR})  AS above_floor
        FROM resolved
        WHERE correct IS NOT NULL AND flat_threshold_hit
    """).fetchone()

    n, above = int(row[0] or 0), int(row[1] or 0)
    return {
        "cohort": cohort,
        "flags": n,
        "above_floor": above,
        "share_pct": (100.0 * above / n) if n else None,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_header(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(title)
    print("=" * 70)


def _verdict(stats: dict, n_a: int, n_b: int, treatment: str, control: str) -> None:
    """
    Turn the interval into a stated conclusion.

    Deliberately refuses to call a null result when the sample cannot
    support one. "Not significantly different" and "demonstrably the same"
    are different findings, and only the second one falsifies the signal.
    """
    _print_header("VERDICT")

    if not stats["usable"]:
        print("  Cannot compare — one or both arms have no resolved markets.")
        print("  Load both cohorts and run `python main.py resolve` first.")
        return

    margin = EQUIVALENCE_MARGIN
    diff_pp = stats["diff"] * 100
    lo_pp, hi_pp = stats["ci_low"] * 100, stats["ci_high"] * 100
    margin_pp = margin * 100

    print(f"  {treatment} hit rate : {stats['p_a'] * 100:.1f}%  (n={n_a} markets)")
    print(f"  {control} hit rate : {stats['p_b'] * 100:.1f}%  (n={n_b} markets)")
    print(f"  Difference        : {diff_pp:+.1f} pp")
    print(f"  {int((1 - SIGNIFICANCE_ALPHA) * 100)}% CI on difference: "
          f"[{lo_pp:+.1f}, {hi_pp:+.1f}] pp")
    print(f"  z = {stats['z']:+.2f}   p = {stats['p_value']:.4f}")

    mde = minimum_detectable_effect(n_a, n_b)
    if mde is not None:
        print(f"  Smallest detectable difference at this sample size: "
              f"{mde * 100:.1f} pp")

    print()

    underpowered = n_a < MIN_RESOLVED_SAMPLE or n_b < MIN_RESOLVED_SAMPLE
    significant = stats["p_value"] < SIGNIFICANCE_ALPHA
    within_margin = abs(lo_pp) <= margin_pp and abs(hi_pp) <= margin_pp

    if underpowered:
        print(f"  INCONCLUSIVE — UNDERPOWERED.")
        print(f"  At least one arm is below {MIN_RESOLVED_SAMPLE} resolved markets.")
        print("  Neither a difference nor an equivalence can be established.")
        print("  Broaden the cohort keyword lists or extend the data range.")
        return

    if significant and stats["diff"] > 0:
        print(f"  SIGNAL SURVIVES THIS TEST.")
        print(f"  {treatment} outperforms {control} by more than chance explains.")
        print("  The effect is not merely 'large trades near close are correct'.")
        print("  Note this rules out ONE alternative explanation, not all of them.")
    elif significant and stats["diff"] < 0:
        print(f"  UNEXPECTED — {control} outperforms {treatment}.")
        print("  Check the contamination diagnostic above: a control arm whose")
        print("  close times lag the real event will score near-perfectly for")
        print("  reasons that have nothing to do with prediction.")
    elif within_margin:
        print(f"  SIGNAL NOT SUPPORTED.")
        print(f"  The difference is statistically indistinguishable from zero AND")
        print(f"  the confidence interval fits within the ±{margin_pp:.0f} pp equivalence")
        print("  margin. The arms perform equivalently, which is what H0 predicts:")
        print("  the detector is picking up large-trades-near-resolution generally,")
        print("  not information asymmetry.")
    else:
        print(f"  INCONCLUSIVE.")
        print(f"  No significant difference, but the CI [{lo_pp:+.1f}, {hi_pp:+.1f}] pp")
        print(f"  extends beyond the ±{margin_pp:.0f} pp equivalence margin — the sample")
        print("  cannot distinguish 'the same' from 'meaningfully different'.")
        print("  This is NOT evidence the signal is bunk. It is a power problem.")


def run(
    treatment: str = "committee",
    control: str = "sports",
    require_onchain: bool | None = None,
) -> dict:
    """
    Run the counter-test comparing two cohorts.

    Returns the comparison statistics so this can be called from a
    notebook without re-parsing stdout.
    """
    cohorts.keywords_for(treatment)
    cohorts.keywords_for(control)

    if require_onchain is None:
        require_onchain = COMPARISON_REQUIRE_ONCHAIN

    conn = duckdb.connect(DB_PATH, read_only=True)

    print(f"\n{'#' * 70}")
    print(f"#  COUNTER-TEST — {treatment} (treatment) vs {control} (control)")
    print(f"#  resolution source: {'on-chain only' if require_onchain else 'on-chain + heuristic'}")
    print(f"{'#' * 70}")

    if not require_onchain:
        print(
            "\n  WARNING: heuristic resolutions are included. Those infer the\n"
            "  winner from late price convergence, which is circular here and\n"
            "  biases the more liquid arm. Set COMPARISON_REQUIRE_ONCHAIN=true."
        )

    # ── 1. Inventory ─────────────────────────────────────────────────────────
    _print_header("1. COHORT INVENTORY")
    # Pass our own read-only connection — DuckDB rejects a second
    # connection to the same file opened with a different configuration.
    inventory = pd.DataFrame(storage.cohort_counts(conn))
    print(inventory.to_string(index=False) if not inventory.empty else "  (no data)")

    # ── 2. Threshold parity ──────────────────────────────────────────────────
    _print_header("2. THRESHOLD PARITY — is the detector doing the same job in both?")
    print(
        "  FLAT_THRESHOLD_USD is an absolute dollar figure. If it selects the\n"
        "  top 0.1% of trades in one cohort and 10% in the other, the arms are\n"
        "  not running the same experiment and the percentile threshold is the\n"
        "  fairer comparator.\n"
    )
    parity = pd.DataFrame([_threshold_parity(conn, c) for c in (treatment, control)])
    print(parity.to_string(index=False))

    # ── 3. Contamination ─────────────────────────────────────────────────────
    _print_header("3. CONTAMINATION CHECK — is the control arm actually a control?")
    print(
        f"  Share of flagged trades executing at price >= {POST_OUTCOME_PRICE_FLOOR},\n"
        "  where the outcome was likely already public. A much higher share in\n"
        "  the control arm suggests its markets close well after the real event\n"
        "  ended, so the window captures post-outcome trading.\n"
    )
    contamination = pd.DataFrame(
        [_contamination(conn, c, require_onchain) for c in (treatment, control)]
    )
    print(contamination.to_string(index=False))

    # ── 4. Headline comparison ───────────────────────────────────────────────
    _print_header("4. MARKET-LEVEL HIT RATE (one vote per market)")

    n_a, c_a = _market_level_counts(conn, treatment, require_onchain)
    n_b, c_b = _market_level_counts(conn, control, require_onchain)

    for label, correct, total in ((treatment, c_a, n_a), (control, c_b, n_b)):
        if total:
            z, p = binomial_test(correct, total)
            flag = "  [UNDERPOWERED]" if total < MIN_RESOLVED_SAMPLE else ""
            print(f"  {label:<12} {correct}/{total} = {correct / total * 100:.1f}%  "
                  f"| vs coin flip: z={z:+.2f} p={p:.4f}{flag}")
        else:
            print(f"  {label:<12} no resolved markets")

    # ── 5. Time × edge crosstab ──────────────────────────────────────────────
    _print_header("5. HIT RATE AND EDGE BY TIME BEFORE CLOSE")
    print(
        "  Compare the arms within each band. A signal that is real should\n"
        "  beat its control in the bands where its edge is largest — if the\n"
        "  two move together across every band, that co-movement is the\n"
        "  microstructure effect, not insider information.\n"
    )

    frames = []
    for label in (treatment, control):
        df = _time_edge_table(conn, label, require_onchain)
        if df.empty:
            continue
        df = df.drop(columns=["_sort"], errors="ignore")
        df.insert(0, "cohort", label)
        frames.append(df)

    if frames:
        print(pd.concat(frames, ignore_index=True).to_string(index=False))
    else:
        print("  (no resolved market-level observations in either cohort)")

    # ── 6. Verdict ───────────────────────────────────────────────────────────
    stats = two_proportion_test(c_a, n_a, c_b, n_b)
    _verdict(stats, n_a, n_b, treatment, control)

    conn.close()

    return {
        "treatment": treatment,
        "control": control,
        "treatment_markets": n_a,
        "treatment_correct": c_a,
        "control_markets": n_b,
        "control_correct": c_b,
        **stats,
    }


if __name__ == "__main__":
    run()
