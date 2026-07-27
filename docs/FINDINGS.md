# Findings

> **Template note:** tables below are populated with verified output from the
> pipeline — reproduce any of them with the command given above each table.
> Sections marked _[WRITE]_ are for your own prose. Delete this note before
> publishing.

**Author:** Samuel Regan
**Status:** Stage 1 complete — hypothesis not supported
**Last updated:** _[DATE]_

---

## 1. Summary

_[WRITE — one paragraph: what you tested, what you found, what you concluded.
Written last, read first. Should stand alone if someone reads nothing else.]_

**Verdict:** Signal not supported. A detector for anomalous large trades before
market close performs equivalently on committee-decided markets (where insider
information is plausible) and sports markets (where it is not), indicating the
effect is market microstructure rather than information asymmetry.

---

## 2. Sample

`python main.py compare`

| Cohort | Markets | On-chain resolved | Flagged trades |
|---|---|---|---|
| committee | 843 | 843 | 4,124 |
| sports | 14,159 | 14,075 | 41,869 |

Coverage period: 2022 → present ([poly_data](https://github.com/warproxxx/poly_data)
`OrderFilled` dumps, ~151M trade rows scanned per cohort).

On-chain resolution coverage is effectively complete, so the price-convergence
heuristic — which would be circular here — is not load-bearing for any figure
in this document.

---

## 3. Headline result

### 3.1 Each cohort against chance

One vote per market (largest flagged trade), flat threshold, on-chain only:

| Cohort | Hit rate | z | p |
|---|---|---|---|
| committee | 93.2% (69/74) | +7.44 | < 0.0001 |
| sports | 92.1% (1,241/1,347) | +30.93 | < 0.0001 |

_[WRITE — note that read in isolation, either row looks like a discovery. This
is the trap the counter-test exists to catch.]_

### 3.2 The counter-test

| Statistic | Value |
|---|---|
| Difference (committee − sports) | +1.1 pp |
| 95% CI on difference | [−4.8, +7.0] pp |
| z | +0.35 |
| p | 0.7284 |
| Equivalence margin (pre-specified) | ±10 pp |
| Minimum detectable effect | 11.7 pp |

The confidence interval falls entirely within the equivalence margin. This is a
positive finding of equivalence, not merely a failure to reject.

_[WRITE — why equivalence ≠ non-significance, and why that distinction is what
makes this a result rather than an inconclusive run.]_

---

## 4. Profitability

`python main.py calibration --cohort <name>`

Edge = win_rate − average execution price = expected profit per $1 staked,
mirroring the largest flagged trade in each market.

| Cohort / subset | Edge | 95% CI | p | n |
|---|---|---|---|---|
| committee, all flagged | +1.41¢ | [−0.04%, +2.85%] | 0.056 | 542 |
| committee, flat only | +0.86¢ | [−2.87%, +4.58%] | 0.65 | 74 |
| sports, all flagged | −0.10¢ | [−0.66%, +0.45%] | 0.72 | 5,736 |

No configuration reaches significance at α = 0.05. Figures are gross of fees,
slippage, market impact, and execution lag.

_[WRITE — the sports arm returning ≈0 across 5,736 markets is evidence the
measurement apparatus works, not just that the signal fails. Worth saying.]_

### 4.1 Edge by time before close

Market-level, flat threshold, on-chain only:

| Cohort | Band | Markets | Hit rate | Avg price | Edge |
|---|---|---|---|---|---|
| committee | 0–1h | 4 | 100.0% | 0.999 | +0.001 |
| committee | 1–6h | 46 | 100.0% | 0.997 | +0.003 |
| committee | 6–24h | 17 | 76.5% | 0.754 | +0.011 |
| committee | 24h+ | 7 | 85.7% | 0.813 | +0.044 |
| sports | 0–1h | 101 | 100.0% | 0.999 | +0.001 |
| sports | 1–6h | 820 | 94.8% | 0.942 | +0.006 |
| sports | 6–24h | 261 | 84.7% | 0.824 | +0.023 |
| sports | 24h+ | 165 | 86.1% | 0.868 | −0.008 |

_[WRITE — the 0–6h bands are near-ceiling at prices ≈0.997–0.999, where
"correct" is close to automatic and edge is ≈0. Any real information content
would have to live in the 6–24h and 24h+ bands, where committee has 24 markets
total — too few to conclude anything.]_

---

## 5. Diagnostics that qualify the result

### 5.1 Post-outcome contamination

Share of flagged trades executing at price ≥ 0.97, where the outcome was
plausibly already public:

| Cohort | Flags | ≥ 0.97 | Share |
|---|---|---|---|
| committee | 164 | 111 | 67.7% |
| sports | 7,105 | 3,633 | 51.1% |

_[WRITE — this diagnostic was built expecting the SPORTS arm to be contaminated
(close times lagging the final whistle). It came back the other way. Both arms
are weighted toward trades placed when the result was effectively known.]_

### 5.2 Threshold parity

`FLAT_THRESHOLD_USD` is an absolute figure; it does not select the same stratum
in cohorts with different liquidity:

| Cohort | Window trades | Flagged | Flag rate | Median flag |
|---|---|---|---|---|
| committee | 52,094 | 4,124 | 7.9% | $75.95 |
| sports | 414,894 | 41,869 | 10.1% | $196.81 |

_[WRITE — flag rates are close enough (7.9% vs 10.1%) that the arms are doing
comparable work, which is what makes the comparison legitimate.]_

---

## 6. Data integrity issue found mid-analysis

A units bug in the trade loader treated raw on-chain amounts as whole shares.
Polymarket tokens use 6 decimals, so `volume_usd` was inflated 1,000,000× —
producing single "trades" of up to $71.7 billion against markets whose entire
lifetime volume was ~$150,000.

Impact: **35,712 of 39,836 committee flagged trades (90%) were artifacts**,
trades of a few dollars pushed past a $1,000 threshold by the scaling error.

The error was compounded by `INSERT OR IGNORE` deduplication: a corrected reload
computed the right values, then silently discarded every row as a duplicate of
the stale one, logging them as inserted.

Before the repair, the same counter-test reported **sports outperforming
committee by 18 pp (p < 0.0001)** — the opposite verdict, from identical code.

_[WRITE — this is worth a paragraph. The diagnostics in §5 are what surfaced it:
a median flagged trade of $5,004,990 against $196 in the control arm was not
plausible as a cohort difference. A single headline percentage would have hidden
it completely.]_

Repair: [`storage.repair_token_decimals()`](../polysignals/core/storage.py) —
idempotent, self-recording, rescales in place and drops rows that were never
signals under correct units.

---

## 7. Limitations

_[WRITE — expand each. Bullets are the skeleton.]_

- **Underpowered treatment arm.** n = 74 markets on the flat threshold; MDE is
  11.7 pp. The equivalence claim holds at ±10 pp but cannot rule out a smaller
  true effect.
- **Deduplication is not declustering.** The market-level unit collapses by
  market and selects the largest trade, not the earliest, so a copy-trade can
  represent a market and time-before-close reflects the herd rather than the
  origin.
- **Keyword-defined universe.** Imprecise at the edges; a supervised classifier
  with reported precision/recall would be the rigorous alternative.
- **Thresholds have no empirical basis.** Chosen a priori to avoid in-sample
  overfitting; sensitivity analysis deferred.
- **Detection, not execution.** No slippage, latency, capital constraints, or
  market impact are modelled.
- **Features deliberately omitted** — Amihud ratio, maker/taker identity,
  entry vs. exit — under a staged-validation approach: establish a signal
  exists before refining it.

---

## 8. Conclusion

_[WRITE — go/no-go, stated plainly. Suggested substance: the coarse signal does
not survive falsification, so refining it (composite scoring, feature weighting,
threshold optimisation) is unwarranted without first revisiting the hypothesis.
What would change the answer: a larger treatment sample in the 6–24h+ bands, and
a cleaner universe definition.]_

---

## 9. Reproducibility

```bash
python main.py counter-test                    # both cohorts → resolve → compare
python main.py calibration --cohort committee  # edge vs. execution price
python main.py analysis --cohort committee     # full breakdown
python -m pytest tests/ -v                     # 38 tests
```

Requires the [poly_data](https://github.com/warproxxx/poly_data) CSV dumps; see
[.env.example](../.env.example). Full runtime for `counter-test` is ~1 hour,
dominated by two passes over the 37 GB `OrderFilled` file and ~14,000 Polygon
RPC calls.
