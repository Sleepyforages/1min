"""
generate_clob_creds.py — Derive or create Polymarket CLOB API credentials.

The CLOB L2 API key/secret/passphrase are DIFFERENT from the Builder API key.
They are cryptographically derived from your private key via the CLOB endpoint.

Run this once on the VPS to get real CLOB credentials, then update .env.

Usage:
    docker exec polymarket-hedge-bot python -m scripts.generate_clob_creds

If your private key is for a PROXY wallet (not your main wallet), also set:
    POLYMARKET_FUNDER_ADDRESS=0xYourMainWalletAddress
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)


def main():
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not pk:
        logger.error("POLYMARKET_PRIVATE_KEY not set in .env")
        sys.exit(1)

    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

    logger.info("=== Polymarket CLOB Credential Generator ===")

    from py_clob_client.client import ClobClient, Signer

    # Derive wallet address from private key
    signer = Signer(pk, chain_id=137)
    signer_address = signer.address()
    logger.info("Signer address (from private key): %s", signer_address)
    if funder:
        logger.info("Funder address (from env):         %s", funder)
    else:
        logger.info("Funder address: not set (using signer as funder)")

    # Build L1-only client (no creds — we're CREATING them)
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        funder=funder or None,
    )

    # ── Step 1: test L1 auth by attempting to create an API key ───────────────
    logger.info("")
    logger.info("Step 1 — Testing L1 auth (attempting create_api_key) ...")
    creds = None
    try:
        creds = client.create_api_key()
        logger.info("L1 auth OK — new CLOB API key created.")
        l1_ok = True
    except Exception as exc:
        err_str = str(exc)
        logger.warning("create_api_key result: %s", err_str)
        # If key already exists we may get a conflict — try derive instead
        if "exist" in err_str.lower() or "conflict" in err_str.lower() or "400" in err_str:
            logger.info("Key may already exist, trying derive_api_key ...")
            try:
                creds = client.derive_api_key()
                logger.info("L1 auth OK — existing CLOB API key derived.")
                l1_ok = True
            except Exception as exc2:
                logger.error("derive_api_key also failed: %s", exc2)
                l1_ok = False
        else:
            logger.error("L1 auth FAILED: %s", exc)
            logger.error("")
            logger.error("Possible causes:")
            logger.error("  1. Private key is for a proxy wallet — set POLYMARKET_FUNDER_ADDRESS")
            logger.error("     to your main MetaMask wallet address (0xcef717...)")
            logger.error("  2. Wallet has not interacted with Polymarket CLOB contracts")
            logger.error("  3. Wrong private key")
            l1_ok = False

    if not l1_ok:
        logger.info("")
        if not funder:
            logger.info("→ Add POLYMARKET_FUNDER_ADDRESS=<your-main-wallet> to .env and retry")
        sys.exit(1)

    # ── Step 2: print credentials ──────────────────────────────────────────
    logger.info("")
    logger.info("Step 2 — CLOB credentials:")
    logger.info("")
    logger.info("  Add these to your .env file (replace the Builder API key):")
    logger.info("")
    logger.info("  POLYMARKET_API_KEY=%s", creds.api_key)
    logger.info("  POLYMARKET_API_SECRET=%s", creds.api_secret)
    logger.info("  POLYMARKET_API_PASSPHRASE=%s", creds.api_passphrase)
    logger.info("")

    # ── Step 3: verify L2 auth with new creds ─────────────────────────────
    logger.info("Step 3 — Verifying L2 auth with new credentials ...")
    try:
        from py_clob_client.clob_types import ApiCreds
        client2 = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            funder=funder or None,
            creds=ApiCreds(
                api_key=creds.api_key,
                api_secret=creds.api_secret,
                api_passphrase=creds.api_passphrase,
            ),
        )
        orders = client2.get_orders()
        logger.info("L2 auth OK. Open orders: %s", orders)
        logger.info("")
        logger.info("All done! Update .env and redeploy.")
    except Exception as exc:
        logger.error("L2 auth verification failed: %s", exc)


if __name__ == "__main__":
    main()
