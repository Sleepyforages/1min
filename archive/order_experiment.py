"""
order_experiment.py — Standalone order placement test.

Tests two order types at the start of a fresh 5m window:
  1. LIMIT BUY at $0.49 on the Up token  (GTC — should fill near open)
  2. MARKET BUY on the Down token        (FOK — fills immediately at best price)

Waits for the next window boundary, then fires both orders simultaneously.

Usage:
    cd /app
    python -m scripts.order_experiment --asset hype [--size 1.0] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s — %(message)s",
)
logger = logging.getLogger(__name__)

CLOB_BASE  = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


# ── Market helpers ─────────────────────────────────────────────────────────────

def find_market(asset: str) -> dict | None:
    """Find the current 5m Up/Down market for the given asset."""
    now_ts       = int(time.time())
    interval_secs = 300
    window_end_ts = ((now_ts + interval_secs) // interval_secs) * interval_secs
    slug          = f"{asset.lower()}-updown-5m-{window_end_ts}"
    logger.info("Looking up slug: %s", slug)

    resp = requests.get(f"{GAMMA_BASE}/events",
                        params={"active": "true", "slug": slug}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        return None

    event  = data[0]
    market = event["markets"][0]
    tokens = market["clobTokenIds"]
    token_ids = json.loads(tokens) if isinstance(tokens, str) else tokens

    return {
        "slug":         slug,
        "question":     event.get("title", ""),
        "condition_id": market.get("conditionId", ""),
        "up_token":     token_ids[0],
        "down_token":   token_ids[1],
        "window_end_ts": window_end_ts,
    }


def get_last_trade_price(token_id: str) -> float:
    resp = requests.get(f"{CLOB_BASE}/last-trade-price",
                        params={"token_id": token_id}, timeout=5)
    if resp.status_code == 200:
        p = resp.json().get("price", "")
        if p:
            return float(p)
    # Fallback: best ask from book (asks sorted worst-first → best = last)
    resp2 = requests.get(f"{CLOB_BASE}/book",
                         params={"token_id": token_id}, timeout=5)
    if resp2.status_code == 200:
        asks = resp2.json().get("asks", [])
        if asks:
            return float(asks[-1]["price"])
    return 0.5


def get_book_summary(token_id: str) -> str:
    resp = requests.get(f"{CLOB_BASE}/book",
                        params={"token_id": token_id}, timeout=5)
    if resp.status_code != 200:
        return f"book error {resp.status_code}"
    b = resp.json()
    bids = b.get("bids", [])
    asks = b.get("asks", [])
    best_bid = float(bids[-1]["price"]) if bids else 0
    best_ask = float(asks[-1]["price"]) if asks else 0
    last     = b.get("last_trade_price", "")
    return (f"best_bid={best_bid:.2f}  best_ask={best_ask:.2f}  "
            f"last_trade={last or 'none'}  tick={b.get('tick_size','?')}")


# ── CLOB client ────────────────────────────────────────────────────────────────

def build_client():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    creds = ApiCreds(
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_API_SECRET"],
        api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
    )
    return ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=pk,
        creds=creds,
    )


# ── Order placement ────────────────────────────────────────────────────────────

def place_limit_buy(client, token_id: str, price: float, size_usd: float) -> dict:
    """Place a GTC limit buy at the specified price."""
    from py_clob_client.clob_types import OrderArgs, OrderType, BUY

    token_size = size_usd / price   # convert USD → token units
    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=token_size,
        side=BUY,
    )
    signed = client.create_order(order_args)
    resp   = client.post_order(signed, OrderType.GTC)
    return resp


def place_market_buy(client, token_id: str, size_usd: float) -> dict:
    """Place a FOK market buy (fills immediately at best available price)."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

    # price=0 → CLOB auto-calculates from order book
    order_args = MarketOrderArgs(token_id=token_id, amount=size_usd, price=0)
    signed     = client.create_market_order(order_args)
    resp       = client.post_order(signed, OrderType.FOK)
    return resp


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket order experiment")
    parser.add_argument("--asset",   default="hype", help="Asset to trade (default: hype)")
    parser.add_argument("--size",    type=float, default=1.0, help="USD size per order")
    parser.add_argument("--dry-run", action="store_true", help="Print orders but don't submit")
    args = parser.parse_args()

    # ── Find market ────────────────────────────────────────────────────────────
    market = find_market(args.asset)
    if not market:
        logger.error("No market found for %s. Aborting.", args.asset)
        sys.exit(1)

    logger.info("Market: %s", market["question"])
    logger.info("Up  token: %s…", market["up_token"][:30])
    logger.info("Down token: %s…", market["down_token"][:30])

    # ── Check current book ────────────────────────────────────────────────────
    logger.info("Up  book: %s", get_book_summary(market["up_token"]))
    logger.info("Down book: %s", get_book_summary(market["down_token"]))

    up_price   = get_last_trade_price(market["up_token"])
    down_price = get_last_trade_price(market["down_token"])
    logger.info("Entry prices — Up: %.3f  Down: %.3f", up_price, down_price)

    if args.dry_run:
        logger.info("[DRY-RUN] Would place:")
        logger.info("  LIMIT BUY Up  @ $0.49  size=$%.2f", args.size)
        logger.info("  MARKET BUY Down  size=$%.2f  (current price: %.3f)", args.size, down_price)
        return

    # ── Wait for next window boundary ─────────────────────────────────────────
    now_ts        = int(time.time())
    window_end_ts = market["window_end_ts"]
    window_start  = window_end_ts - 300
    seconds_until = window_start - now_ts

    if seconds_until > 0:
        logger.info("Waiting %ds for next window to open at %s UTC …",
                    seconds_until,
                    datetime.fromtimestamp(window_start, tz=timezone.utc).strftime("%H:%M:%S"))
        time.sleep(max(0, seconds_until - 1))
        # Spin-wait for the exact boundary
        while int(time.time()) < window_start:
            time.sleep(0.05)
    else:
        logger.info("Window already open (%ds in). Placing orders now.", -seconds_until)

    logger.info("Window open! Placing orders …")

    # ── Build client ──────────────────────────────────────────────────────────
    try:
        client = build_client()
        logger.info("CLOB client initialised OK")
    except Exception as exc:
        logger.error("Failed to build CLOB client: %s", exc)
        sys.exit(1)

    # ── Order 1: Limit buy Up @ 0.49 ─────────────────────────────────────────
    logger.info("Placing LIMIT BUY Up @ $0.49  size=$%.2f …", args.size)
    try:
        limit_resp = place_limit_buy(client, market["up_token"], price=0.49, size_usd=args.size)
        logger.info("LIMIT BUY response: %s", limit_resp)
    except Exception as exc:
        logger.error("LIMIT BUY failed: %s", exc)

    # ── Order 2: Market buy Down ──────────────────────────────────────────────
    logger.info("Placing MARKET BUY Down  size=$%.2f …", args.size)
    try:
        market_resp = place_market_buy(client, market["down_token"], size_usd=args.size)
        logger.info("MARKET BUY response: %s", market_resp)
    except Exception as exc:
        logger.error("MARKET BUY failed: %s", exc)

    # ── Check book again after orders ─────────────────────────────────────────
    time.sleep(2)
    logger.info("Post-order Up  book: %s", get_book_summary(market["up_token"]))
    logger.info("Post-order Down book: %s", get_book_summary(market["down_token"]))


if __name__ == "__main__":
    main()
