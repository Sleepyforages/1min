"""
redeem_positions.py — Redeem all redeemable winning positions on Polygon CTF.

Calls redeemPositions(collateralToken, parentCollectionId, conditionId, indexSets)
on the Gnosis CTF contract for each winning conditionId.

Usage:
    python -m scripts.redeem_positions
    # or from repo root:
    python scripts/redeem_positions.py
"""

from __future__ import annotations

import os
import sys
import time
from collections import defaultdict

import requests
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

WALLET          = "0x347C80CE3a2786AE2e7f2BcE57f64aD032904A63"
CTF_CONTRACT    = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Gnosis CTF on Polygon
USDC_E          = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # collateral token
PARENT_COLL_ID  = b"\x00" * 32                                  # root parentCollectionId
RPC_URL         = "https://polygon-bor-rpc.publicnode.com"

CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    }
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_winning_positions() -> dict[str, list[int]]:
    """Return {conditionId: [indexSet, ...]} for all redeemable winning positions."""
    r = requests.get(
        "https://data-api.polymarket.com/positions",
        params={"user": WALLET, "sizeThreshold": "0.01"},
        timeout=15,
    )
    r.raise_for_status()
    positions = r.json()

    by_cond: dict[str, list[int]] = defaultdict(list)
    for p in positions:
        if p.get("redeemable") and p.get("curPrice", 0) == 1.0:
            cid = p["conditionId"]
            outcome_index = p["outcomeIndex"]   # 0=Up, 1=Down
            index_set = 2 ** outcome_index      # bitmask: index 0 → 1, index 1 → 2
            if index_set not in by_cond[cid]:
                by_cond[cid].append(index_set)

    return dict(by_cond)


def redeem_all(private_key: str, dry_run: bool = False) -> None:
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        print("ERROR: Cannot connect to Polygon RPC")
        sys.exit(1)

    account = w3.eth.account.from_key(private_key)
    signer  = account.address
    print(f"Signer    : {signer}")
    print(f"RPC       : {RPC_URL}")

    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_CONTRACT),
        abi=CTF_ABI,
    )

    winning = fetch_winning_positions()
    print(f"Winning conditionIds to redeem: {len(winning)}")
    if not winning:
        print("Nothing to redeem — exiting.")
        return

    total_gas_spent = 0
    success = 0
    failed  = 0

    for i, (cid, index_sets) in enumerate(winning.items(), 1):
        cid_bytes = bytes.fromhex(cid.removeprefix("0x"))
        print(f"\n[{i}/{len(winning)}] conditionId={cid}  indexSets={index_sets}")

        if dry_run:
            print("  [DRY RUN] skipping tx")
            continue

        try:
            # Use latest confirmed nonce (not pending) to avoid nonce collisions
            nonce = w3.eth.get_transaction_count(signer, "latest")
            # 2× gas price to ensure fast inclusion on Polygon
            gas_price = int(w3.eth.gas_price * 2)

            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_E),
                PARENT_COLL_ID,
                cid_bytes,
                index_sets,
            ).build_transaction({
                "from":     signer,
                "nonce":    nonce,
                "gasPrice": gas_price,
                "gas":      120_000,
            })

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  TX sent: 0x{tx_hash.hex()}")

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] == 1:
                gas_used = receipt["gasUsed"]
                total_gas_spent += gas_used
                print(f"  OK  gasUsed={gas_used}  block={receipt['blockNumber']}")
                success += 1
            else:
                print(f"  REVERTED  receipt={receipt}")
                failed += 1

            # Wait for confirmation to propagate before next tx
            time.sleep(3)

        except Exception as exc:
            print(f"  ERROR: {exc}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Done  success={success}  failed={failed}  gas_used={total_gas_spent}")
    print(f"Check wallet {WALLET} for USDC.e balance")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not private_key:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in environment / .env")
        sys.exit(1)

    dry = "--dry-run" in sys.argv
    if dry:
        print("=== DRY RUN — no transactions will be sent ===\n")

    redeem_all(private_key, dry_run=dry)
