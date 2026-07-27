"""
Single entry point for the Polymarket signal project.

The project runs one detector over two market universes ("cohorts") and
compares them — see polysignals/core/cohorts.py. Most commands take
--cohort; the counter-test compares two.

Usage:
    python main.py                             # full pipeline on the default cohort
    python main.py counter-test                # load BOTH cohorts, resolve, compare

    python main.py load --cohort sports        # CSV dumps → DuckDB (+ signal detection)
    python main.py resolve                     # on-chain resolutions (all cohorts)
    python main.py stats --cohort sports       # quick hit-rate report
    python main.py analysis --cohort sports    # full analysis queries
    python main.py calibration --cohort sports # is the signal priced in already?
    python main.py compare                     # the counter-test comparison

    python main.py reset-signals               # clear derived tables, ALL cohorts
    python main.py reset-cohort --cohort sports   # clear one arm only
    python main.py resolve-gamma               # optional cross-check via Gamma API
    python main.py enrich --cohort sports      # optional price-convergence winners
    python main.py backtest-api --cohort sports   # API-driven backtest (no CSVs)
    python main.py live --cohort committee     # live scanner for markets closing soon

Configuration lives in polysignals/config.py; machine-specific paths, the
Polygon RPC, and any tuned thresholds come from .env (see .env.example).
"""

import argparse
import logging
import sys

# Force UTF-8 on the standard streams before argparse can print help text
# containing non-ASCII glyphs (arrows, box rules) — otherwise a Windows
# cp1252 console raises UnicodeEncodeError. Mirrors polysignals/__init__,
# which covers direct `python -m polysignals...` runs.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

from polysignals.config import DEFAULT_COHORT  # noqa: E402
from polysignals.core import cohorts  # noqa: E402


# Commands are (function, takes_cohort). Anything cohort-agnostic — on-chain
# resolution, the Gamma cross-check, the comparison itself — is marked False
# so passing --cohort to it is rejected loudly instead of silently ignored.

def cmd_load(cohort: str) -> None:
    from polysignals.backtest import csv_loader
    csv_loader.run(cohort=cohort)


def cmd_reset_signals() -> None:
    from polysignals.core import storage
    storage.reset_signals()


def cmd_reset_cohort(cohort: str) -> None:
    from polysignals.core import storage
    storage.reset_cohort(cohort, keep_markets=False)


def cmd_resolve() -> None:
    from polysignals.backtest import fetch_resolutions
    fetch_resolutions.run()


def cmd_resolve_gamma() -> None:
    from polysignals.backtest import gamma_resolutions
    gamma_resolutions.run()


def cmd_enrich(cohort: str) -> None:
    from polysignals.backtest import enrich_markets
    enrich_markets.run(cohort=cohort)


def cmd_stats(cohort: str) -> None:
    from polysignals.backtest import csv_loader
    csv_loader.hit_rate_report(cohort=cohort)


def cmd_analysis(cohort: str) -> None:
    from polysignals.backtest import analysis
    analysis.run(cohort=cohort)


def cmd_calibration(cohort: str) -> None:
    from polysignals.backtest import calibration
    calibration.run(cohort=cohort)


def cmd_compare() -> None:
    from polysignals.backtest import compare
    compare.run()


def cmd_backtest_api(cohort: str) -> None:
    from polysignals.backtest import api_backtest
    api_backtest.run(cohort=cohort)


def cmd_live(cohort: str) -> None:
    from polysignals.live import scanner
    scanner.run(cohort=cohort)


def _banner(title: str) -> None:
    logger.info("")
    logger.info("═" * 60)
    logger.info(title)
    logger.info("═" * 60)


def cmd_pipeline(cohort: str) -> None:
    """The whole backtest for one cohort: load CSVs, fetch real
    resolutions, report accuracy."""
    steps = (
        (f"STEP 1/4 — Load CSVs into DuckDB [{cohort}]", lambda: cmd_load(cohort)),
        ("STEP 2/4 — Fetch on-chain resolutions",         cmd_resolve),
        (f"STEP 3/4 — Hit-rate report [{cohort}]",        lambda: cmd_stats(cohort)),
        (f"STEP 4/4 — Full analysis [{cohort}]",          lambda: cmd_analysis(cohort)),
    )
    for title, step in steps:
        _banner(title)
        step()


def cmd_counter_test() -> None:
    """
    The full counter-test, end to end.

    Loads both arms, resolves everything on-chain in a single pass, then
    compares. Resolution runs once after both loads rather than per cohort
    because it reads whatever is in the markets table and is the slow,
    rate-limited step — doing it twice would double the RPC time for no
    additional coverage.
    """
    from polysignals.backtest import compare

    treatment, control = "committee", "sports"

    for i, cohort in enumerate((treatment, control), start=1):
        _banner(f"STEP {i}/4 — Load {cohort} markets")
        cmd_load(cohort)

    _banner("STEP 3/4 — Fetch on-chain resolutions (both cohorts)")
    cmd_resolve()

    _banner("STEP 4/4 — Counter-test comparison")
    compare.run(treatment=treatment, control=control)


# name → (handler, takes_cohort)
COMMANDS: dict[str, tuple] = {
    "pipeline":      (cmd_pipeline,      True),
    "counter-test":  (cmd_counter_test,  False),
    "load":          (cmd_load,          True),
    "reset-signals": (cmd_reset_signals, False),
    "reset-cohort":  (cmd_reset_cohort,  True),
    "resolve":       (cmd_resolve,       False),
    "resolve-gamma": (cmd_resolve_gamma, False),
    "enrich":        (cmd_enrich,        True),
    "stats":         (cmd_stats,         True),
    "analysis":      (cmd_analysis,      True),
    "calibration":   (cmd_calibration,   True),
    "compare":       (cmd_compare,       False),
    "backtest-api":  (cmd_backtest_api,  True),
    "live":          (cmd_live,          True),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="pipeline",
        choices=sorted(COMMANDS),
        help="pipeline stage to run (default: the full pipeline)",
    )
    parser.add_argument(
        "--cohort",
        default=None,
        choices=cohorts.available(),
        help=f"market universe to operate on (default: {DEFAULT_COHORT})",
    )
    args = parser.parse_args(argv)

    handler, takes_cohort = COMMANDS[args.command]

    if args.cohort is not None and not takes_cohort:
        parser.error(
            f"`{args.command}` does not take --cohort "
            f"(it operates across all cohorts)."
        )

    if takes_cohort:
        handler(args.cohort or DEFAULT_COHORT)
    else:
        handler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
