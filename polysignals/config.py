"""
Central configuration.

Everything that defines *what the experiment is* lives here: the market
universes, the detection thresholds, the bucket boundaries used in
analysis. Nothing strategy-revealing should be hardcoded in the pipeline
modules — if a number changes what the signal flags or where the edge
appears, it belongs in this file.

Every such value is overridable from the environment, so the tuned
figures can live in an untracked .env while the repo stays public (see
.env.example). The defaults below are the working values; replace them
with placeholders if you want the repository to reveal only the method
and not the parameters.

Machine-specific values (file paths, RPC endpoint) are read from the
environment too.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


# ── Environment helpers ───────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str]) -> list[str]:
    """
    Accepts either a JSON array or a plain comma-separated string, so a
    .env line can be written either way. Always returns lowercase,
    whitespace-stripped entries — keyword matching is case-insensitive.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return [k.lower() for k in default]
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError
    except ValueError:
        parsed = raw.split(",")
    return [str(p).strip().lower() for p in parsed if str(p).strip()]


def _env_floats(name: str, default: list[float]) -> list[float]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError
        return [float(p) for p in parsed]
    except (ValueError, TypeError):
        return default


# ── Storage ───────────────────────────────────────────────────────────────────

DB_PATH = os.getenv("DB_PATH", str(PROJECT_ROOT / "data" / "polymarket.duckdb"))

# ── Backtest inputs: local CSV dumps of on-chain OrderFilled events ───────────
# Produced with https://github.com/warproxxx/poly_data (see poly_data/).

ORDERFILLED_CSV_PATH = os.getenv("ORDERFILLED_CSV_PATH", "")
MARKETS_CSV_PATH = os.getenv("MARKETS_CSV_PATH", "")

# Rows per pandas chunk when streaming the (large) orderFilled CSV.
# Performance only — not strategy.
CSV_CHUNK_SIZE = _env_int("CSV_CHUNK_SIZE", 100_000)


# ── Signal parameters ─────────────────────────────────────────────────────────

SIGNAL_WINDOW_HOURS = _env_float("SIGNAL_WINDOW_HOURS", 48)
FLAT_THRESHOLD_USD = _env_float("FLAT_THRESHOLD_USD", 1_000)
TOP_PCT_THRESHOLD = _env_float("TOP_PCT_THRESHOLD", 0.10)
MIN_MARKET_VOLUME = _env_float("MIN_MARKET_VOLUME", 1_000)  # USD

# Polymarket outcome tokens and USDC both use 6 decimals on-chain, so the
# raw makerAmountFilled / takerAmountFilled values in the OrderFilled CSV
# are integer base units. Divide by this to get human share/dollar amounts.
TOKEN_DECIMALS = 1_000_000


# ── Market universes (cohorts) ────────────────────────────────────────────────
# Each cohort is a keyword list matched as a substring against the lowercased
# market question. See polysignals/core/cohorts.py for how a market is
# assigned to exactly one cohort.

COMMITTEE_KEYWORDS = _env_list("COMMITTEE_KEYWORDS", [
    "oscar", "academy award", "emmy", "grammy", "bafta",
    "golden globe", "nobel", "pulitzer", "hall of fame",
    "best picture", "best director", "best actor", "best actress",
    "best album", "best song", "best film", "wins the award",
    "win the award", "awarded", "trophy",
])

# Control universe for the counter-test: markets whose outcome is decided
# by play on a field, not by a panel's private deliberation. There is no
# plausible "insider" for who wins a basketball game, so any signal that
# performs equally here is measuring market microstructure, not
# information asymmetry.
#
# Chosen for PRECISION over recall. A false positive here (a politics or
# awards market leaking into the control arm) silently contaminates the
# null and invalidates the whole comparison; a false negative just costs
# sample size. So: unambiguous league/competition nouns only. Deliberately
# excluded — " vs ", which catches political matchups and debates, and
# the Olympics, which contain judged events (figure skating, gymnastics,
# diving) that are exactly the panel-decided structure being controlled
# against.
SPORTS_KEYWORDS = _env_list("SPORTS_KEYWORDS", [
    # Leagues and governing bodies
    "nba", "nfl", "mlb", "nhl", "ncaa", "mls",
    "premier league", "la liga", "serie a", "bundesliga", "ligue 1",
    "champions league", "europa league", "uefa", "fifa",
    "ufc", "formula 1", "nascar", "atp", "wta", "pga",
    # Competitions and finals
    "super bowl", "world series", "stanley cup", "nba finals",
    "world cup", "march madness", "wimbledon", "us open",
    "french open", "australian open", "grand slam", "the masters",
    "grand prix",
    # Match-outcome phrasing
    "win the game", "win the match", "beat the",
    "make the playoffs", "win their game",
])

COHORTS: dict[str, list[str]] = {
    "committee": COMMITTEE_KEYWORDS,
    "sports": SPORTS_KEYWORDS,
}

# The cohort the original experiment ran on. Used as the default for CLI
# commands and to backfill rows written before cohorts existed.
DEFAULT_COHORT = os.getenv("DEFAULT_COHORT", "committee")


# ── Resolution thresholds ─────────────────────────────────────────────────────

# enrich_markets.py: last trade price at or above this is treated as a
# winner by the local price-convergence heuristic. Weakest resolution
# source — see compare.py for why it is excluded from the counter-test.
HEURISTIC_WIN_THRESHOLD = _env_float("HEURISTIC_WIN_THRESHOLD", 0.90)

# gamma_resolutions.py / polymarket_api.py: an outcome price at or above
# this, alongside umaResolutionStatus == "resolved", identifies the winner.
RESOLVED_PRICE_THRESHOLD = _env_float("RESOLVED_PRICE_THRESHOLD", 0.99)


# ── Analysis parameters ───────────────────────────────────────────────────────

# Upper bounds (hours before close) for the time buckets in analysis.py.
# Anything beyond the last bound falls into a final open-ended bucket.
TIME_BUCKETS_HOURS = _env_floats("TIME_BUCKETS_HOURS", [1, 6, 24])

# Upper bounds (USD) for trade-size buckets.
SIZE_BUCKETS_USD = _env_floats("SIZE_BUCKETS_USD", [10_000, 50_000, 100_000])

# Upper bounds for execution-price buckets in calibration.py. These are
# where edge is measured, so the boundaries themselves are informative.
PRICE_BUCKETS = _env_floats("PRICE_BUCKETS", [0.60, 0.70, 0.80, 0.90, 0.95, 0.99])

# Two-sided significance level for the binomial and two-proportion tests.
SIGNIFICANCE_ALPHA = _env_float("SIGNIFICANCE_ALPHA", 0.05)

# Below this many resolved observations, report but warn — a hit rate on
# a handful of markets is not evidence of anything.
MIN_RESOLVED_SAMPLE = _env_int("MIN_RESOLVED_SAMPLE", 20)


# ── Counter-test parameters ───────────────────────────────────────────────────

# The counter-test compares cohorts only on markets with an on-chain
# resolution. The price-convergence heuristic infers the winner FROM late
# prices, which is circular when the hypothesis under test is about late
# trades predicting outcomes — and the circularity bites harder in the
# more liquid control arm, which would fake a null result.
COMPARISON_REQUIRE_ONCHAIN = _env_bool("COMPARISON_REQUIRE_ONCHAIN", True)

# Contamination diagnostic. A flagged trade executing above this price is
# very likely made when the outcome is already publicly known — for sports
# especially, if the market's close time lags the final whistle. A control
# arm with a high share of these is not a clean control.
POST_OUTCOME_PRICE_FLOOR = _env_float("POST_OUTCOME_PRICE_FLOOR", 0.97)

# How close two cohorts' hit rates must be, as a proportion, before the
# counter-test will call them equivalent. Expressed as an absolute
# difference: 0.10 means "within 10 percentage points".
#
# This exists because "not significantly different" is not the same claim
# as "the same". With a small sample nothing is significantly different
# from anything, so a bare p-value would let an underpowered run masquerade
# as a falsification. The verdict only concludes the signal is unsupported
# when the confidence interval on the difference fits INSIDE this margin.
EQUIVALENCE_MARGIN = _env_float("EQUIVALENCE_MARGIN", 0.10)


# ── Polymarket APIs (live side + Gamma resolution cross-check) ────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
MARKETS_PAGE_SIZE = 100
TRADES_PAGE_SIZE = 500
REQUEST_TIMEOUT = 10

# Data API pagination gives out well before this on liquid markets; the cap
# stops a single market from monopolising a run.
MAX_TRADE_OFFSET = _env_int("MAX_TRADE_OFFSET", 2500)

# How far back the API-driven backtest walks resolved markets.
BACKTEST_START_CUTOFF = os.getenv("BACKTEST_START_CUTOFF", "2022-01-01T00:00:00Z")


# ── On-chain resolution source ────────────────────────────────────────────────
# Gnosis ConditionalTokens on Polygon mainnet — the contract Polymarket
# settles against. Any Polygon RPC works; the default is a public one.

POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com")
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
