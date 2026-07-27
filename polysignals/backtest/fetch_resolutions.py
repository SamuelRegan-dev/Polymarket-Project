"""
AI CODE
Ground-truth market resolutions, read directly from the blockchain.

Polymarket settles every market through Gnosis's ConditionalTokens
contract on Polygon (CTF_ADDRESS in config). When UMA's oracle finalises
an outcome, a reportPayouts() transaction writes payout numerators into
the contract — the same values winning shares redeem against for USDC.
Reading them back is authoritative in a way no API response is: these are
recorded resolutions, not inferences from price convergence.

Why not an API:
  - The Graph subgraphs — dead end. Polymarket migrated off subgraphs in
    April 2026; the public deployments return stale/incomplete data.
  - Gamma API — off-chain metadata; useful as a cross-check
    (gamma_resolutions.py) but not the settlement source itself.

Per market (via any Polygon RPC, no API key needed):
  payoutDenominator(conditionId) == 0  → not resolved yet (mappings never
                                         revert, so this is one cheap call)
  otherwise payoutNumerators[0] vs [1] → winner is answer1 or answer2;
  equal numerators is a rare 50/50 UMA ruling, stored as storage.TIE and
  excluded from directional accuracy.

Writes markets.onchain_winner in DuckDB. Safe to re-run: only markets
still missing a resolution are queried, so an interrupted run resumes
where it left off.
"""

from __future__ import annotations

import logging
import re
import time

from web3 import Web3
from web3.exceptions import ContractLogicError

from polysignals.config import CTF_ADDRESS, POLYGON_RPC
from polysignals.core import storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Just the two view functions we call — no need for the full CTF ABI.
CTF_ABI = [
    {
        "name": "payoutDenominator",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "payoutNumerators",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}, {"name": "", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

CONDITION_ID_RE = re.compile(r"^0x[0-9a-f]{64}$")

RETRIES = 3
RETRY_BACKOFF_S = 2.0
CALL_PAUSE_S = 0.05      # politeness pause between markets on public RPCs
PROGRESS_EVERY = 25


def _connect() -> Web3:
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        raise ConnectionError(
            f"Cannot reach Polygon RPC at {POLYGON_RPC} — "
            "set POLYGON_RPC in .env to a working endpoint."
        )
    return w3


def _call_with_retry(bound_fn):
    """eth_call with retries on transport errors. Contract reverts are NOT
    retried — they mean the condition doesn't exist / has fewer outcome
    slots, which retrying can't fix."""
    for attempt in range(1, RETRIES + 1):
        try:
            return bound_fn.call()
        except ContractLogicError:
            raise
        except Exception as exc:
            if attempt == RETRIES:
                raise
            logger.warning("  RPC hiccup (attempt %d/%d): %s", attempt, RETRIES, exc)
            time.sleep(RETRY_BACKOFF_S * attempt)


def fetch_payouts(contract, condition_id: str) -> tuple[int, int] | None:
    """
    Payout numerators for a binary condition, or None if not resolved yet.

    payoutDenominator is a mapping — it returns 0 (never reverts) for
    unresolved or unknown conditions, so it doubles as the cheap
    "resolved?" filter before touching the numerator array.
    """
    cid = bytes.fromhex(condition_id[2:])
    denominator = _call_with_retry(contract.functions.payoutDenominator(cid))
    if denominator == 0:
        return None
    n0 = _call_with_retry(contract.functions.payoutNumerators(cid, 0))
    n1 = _call_with_retry(contract.functions.payoutNumerators(cid, 1))
    return n0, n1


def run() -> dict:
    storage.initialise()

    rows = storage.get_markets_needing_resolution()
    logger.info("Markets needing resolution: %d", len(rows))
    if not rows:
        logger.info("Nothing to do.")
        return {"resolved": 0, "unresolved": 0, "ties": 0, "invalid": 0, "errors": 0}

    w3 = _connect()
    logger.info("Connected to Polygon (chain id %d) via %s", w3.eth.chain_id, POLYGON_RPC)
    contract = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)

    resolved = unresolved = ties = invalid = errors = 0

    for i, row in enumerate(rows, 1):
        condition_id = str(row.get("condition_id") or "").strip().lower()
        if not CONDITION_ID_RE.match(condition_id):
            invalid += 1
            logger.warning("  Bad condition_id for market %s: %r", row.get("market_id"), condition_id)
            continue

        try:
            payouts = fetch_payouts(contract, condition_id)
        except ContractLogicError:
            errors += 1
            logger.warning("  Contract revert for %s — unknown or non-binary condition", condition_id)
            continue
        except Exception as exc:
            errors += 1
            logger.warning("  RPC failure for %s: %s", condition_id, exc)
            continue

        if payouts is None:
            unresolved += 1
        else:
            n0, n1 = payouts
            if n0 > n1:
                winner = str(row.get("answer1") or "Yes")
            elif n1 > n0:
                winner = str(row.get("answer2") or "No")
            else:
                winner = storage.TIE
                ties += 1
            storage.update_onchain_winner(row["market_id"], winner)
            if winner != storage.TIE:
                resolved += 1

        if i % PROGRESS_EVERY == 0:
            logger.info("  %d/%d checked | resolved=%d unresolved=%d errors=%d",
                        i, len(rows), resolved, unresolved, errors)

        time.sleep(CALL_PAUSE_S)

    logger.info("=" * 60)
    logger.info("ON-CHAIN RESOLUTION FETCH COMPLETE")
    logger.info("  Resolved (winner)     : %d", resolved)
    logger.info("  50/50 UMA rulings     : %d", ties)
    logger.info("  Not resolved on-chain : %d", unresolved)
    logger.info("  Invalid condition ids : %d", invalid)
    logger.info("  RPC/contract errors   : %d", errors)
    logger.info("=" * 60)
    if errors:
        logger.info("Errored markets are untouched — re-running retries just those.")

    return {"resolved": resolved, "unresolved": unresolved, "ties": ties,
            "invalid": invalid, "errors": errors}


if __name__ == "__main__":
    run()
