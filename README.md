# Polymarket Committee-Market Signals

*full paper to be commited*

Research pipeline crude test for the hypothesis that large anomalous trades made close to market resolution on markets whereby a committee determines the outcome (Nobel Prize awards, who lives/dies in Stranger Things final season, etc.) tend to be correct.

The intuition is that for committee markets insiders exist by construction, as
the panel/staff can know the outcome before the public does. If any of
them trade on it, their size and timing should leave a statistical fingerprint.

**Headline result: the hypothesis was not supported.** The detector performs
just as well on sports markets, where insider information is impossible. See
[Findings](#findings) below and [docs/FINDINGS.md](docs/FINDINGS.md).

## How it works

```
                    ┌──────────────────────────────┐
                    │       polysignals/core        │
                    │ signals.py  – detectors       │
                    │ cohorts.py  – market universes│
                    │ storage.py  – DuckDB layer    │
                    │ polymarket_api.py – API client│
                    └──────────┬───────────────────┘
             ┌─────────────────┴──────────────────┐
   ┌─────────┴──────────┐              ┌──────────┴──────────┐
   │ polysignals/backtest│              │  polysignals/live   │
   │ csv_loader          │              │  scanner            │
   │ fetch_resolutions   │              │  (markets closing   │
   │ gamma_resolutions   │              │   within 48h)       │
   │ enrich_markets      │              └─────────────────────┘
   │ analysis            │
   │ calibration         │
   │ compare  ← counter-test
   └─────────────────────┘
```

- **Backtest side** replays historical on-chain `OrderFilled` events (CSV dumps
  built with [poly_data](https://github.com/warproxxx/poly_data)) through the
  signal detectors and measures directional accuracy against real resolutions.
- **Live side** scans markets closing within the signal window and records
  flagged trades as they happen — same detectors, same storage.

### Cohorts

The same detector runs over two market universes, defined by keyword matching on
question text ([`core/cohorts.py`](polysignals/core/cohorts.py)):

| Cohort | Outcome decided by | Insider information |
|---|---|---|
| `committee` (treatment) | A closed panel deliberating in private | Plausible |
| `sports` (control) | Play on a field | Implausible by construction |

Assignment is **exclusive** — a market matching both lists is dropped from both.
Without this, the same market would appear on both sides of the comparison, and
the `markets` primary key would silently discard the duplicate on insert.

### Signals

Within the final 48h before market close (`SIGNAL_WINDOW_HOURS`), a trade is
flagged when it hits either threshold:

| Signal | Definition |
|---|---|
| Flat | trade notional ≥ $1,000 (`FLAT_THRESHOLD_USD`) |
| Percentile | top 10% of window trades by notional (`TOP_PCT_THRESHOLD`) |

Each flag also records an Amihud-style price-impact ratio (|Δprice| / volume)
and hours-before-close, for later slicing. Thresholds are fixed a priori rather
than fitted, to avoid optimising against the sample before establishing the
signal exists at all.

### Ground-truth resolutions (the hard part)

Knowing who *actually won* each market turned out to be the least obvious piece:

1. **Price convergence** (`enrich_markets.py`) — "last trade ≥ 0.90 wins". Easy,
   but circular: markets can trade at 0.92 and resolve the other way, and that
   gap is exactly what this project measures. Kept only as a fallback, and
   excluded entirely from the counter-test.
2. **The Graph subgraphs** — the classic answer, now a dead end: Polymarket
   migrated off subgraphs in April 2026 and public deployments are stale.
3. **Gamma API** (`gamma_resolutions.py`) — Polymarket's metadata API exposes
   `umaResolutionStatus` + `outcomePrices`; used as an independent cross-check.
4. **On-chain payouts** (`fetch_resolutions.py`) — the source of truth. Every
   Polymarket market settles through Gnosis's ConditionalTokens contract on
   Polygon; when UMA finalises an outcome, `reportPayouts()` writes payout
   numerators into contract storage — the same values winning shares redeem
   against for USDC. The pipeline reads `payoutNumerators` /
   `payoutDenominator` directly over any public Polygon RPC. No API key, no
   indexer, no trust in anyone's database.

On-chain coverage on the current sample is effectively complete (745/745
committee, 13,662/13,745 sports), so the circular heuristic is not load-bearing
for any reported figure.

## Findings

Sample: **745 committee markets**, **13,745 sports markets**, all resolved
against on-chain payouts. Reproduce with `python main.py compare`.

<!-- RESULTS -->
### The counter-test decides it

One vote per market (its largest flagged trade), on-chain resolutions only:

| Cohort | Market-level hit rate | vs. coin flip |
|---|---|---|
| Committee (treatment) | **96.3%** (52/54) | z = +6.80, p < 0.0001 |
| Sports (control) | **92.1%** (1,159/1,259) | z = +29.85, p < 0.0001 |

Both beat chance decisively. Read alone, either one looks like a discovery.
Compared against each other:

| Statistic | Value |
|---|---|
| Difference (committee − sports) | **+4.2 pp** |
| 95% CI on difference | [−1.0, +9.5] pp |
| p-value | 0.25 |
| Minimum detectable effect | 13.6 pp |

**Verdict: signal not supported.** The confidence interval falls entirely inside
a pre-specified ±10 pp equivalence margin, so this is a positive finding of
equivalence — not merely a failure to find a difference. A detector that
performs identically where insiders cannot exist is measuring market
microstructure ("large trades near resolution tend to be correct"), not
information asymmetry.

### Profitability

Mirroring the largest flagged trade in each market at its own execution price:

| Cohort / subset | Edge per $1 staked | 95% CI | p | n |
|---|---|---|---|---|
| Committee, all flagged | +1.22¢ | [−0.37%, +2.81%] | 0.13 | 470 |
| Committee, flat threshold only | −0.41¢ | [−3.95%, +3.13%] | 0.82 | 54 |
| Sports, all flagged | −0.02¢ | [−0.59%, +0.55%] | 0.93 | 5,474 |

No configuration reaches significance. All figures are **gross** of fees,
slippage, and market impact — any of which plausibly exceeds a sub-1% edge when
copying a large trade into a thin awards market.
<!-- /RESULTS -->

Full write-up, including the diagnostics that qualify these numbers:
**[docs/FINDINGS.md](docs/FINDINGS.md)**.

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; use bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env            # then point the CSV paths at your dumps
python main.py counter-test     # load both cohorts → resolve → compare
```

Individual stages:

```bash
python main.py load --cohort committee   # CSV dumps → DuckDB (+ detection)
python main.py load --cohort sports      # the control arm
python main.py resolve                   # on-chain resolutions (all cohorts)
python main.py compare                   # the counter-test
python main.py analysis --cohort committee   # full breakdowns + significance
python main.py calibration --cohort committee # edge vs. execution price
python main.py stats --cohort committee  # quick hit-rate report
python main.py live --cohort committee   # scan markets closing within 48h
```

Re-running a load after changing signal logic requires clearing the cohort
first — inserts dedupe on `(market_id, token_id, trade_timestamp)`, so stale
rows would otherwise survive and silently win:

```bash
python main.py reset-cohort --cohort committee
```

Tests:

```bash
python -m pytest tests/ -v
```

## Data

- `data/polymarket.duckdb` (gitignored) — markets, flagged trades, summaries.
- Input CSVs are multi-GB on-chain event dumps (~37 GB for the full
  `OrderFilled` history), not stored in the repo; build them with
  [poly_data](https://github.com/warproxxx/poly_data) (included as a submodule)
  and point `.env` at them.

## Honest caveats

- **Both cohorts skew to near-certain prices.** 79% of committee flags and 50%
  of sports flags execute at ≥ 0.97, where being "correct" is close to
  automatic. Neither arm cleanly isolates pre-outcome prediction.
- **The market-level unit is deduplication, not declustering.** It collapses by
  market and picks the *largest* trade, not the *earliest* — so a copy-trade
  following an insider can become the market's representative observation, and
  the reported hours-before-close reflects the herd, not the origin.
- **Committee is underpowered** at n=54 markets on the flat threshold; the
  minimum detectable effect is 13.6 pp. The equivalence verdict holds at the
  ±10 pp margin, but a smaller true effect could not be ruled out.
- **The universe is keyword-defined**, so it is imprecise at the edges. A
  learned classifier would be the rigorous alternative; it was not warranted
  before establishing any signal existed.
- Binary markets only; multi-outcome committee markets are represented as
  their binary legs.

## License

MIT
