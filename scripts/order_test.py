"""
order_test.py — Two mechanical tests:

  Test A: Place a $1 order (2 shares @ $0.50) bypassing the 5-share minimum.
          Verifies whether the CLOB actually enforces the minimum itself.

  Test B: Place an order on the NEXT 5-min window (before it opens).
          Verifies whether pre-market orders are accepted or rejected.

Both tests use BTC only. Both cancel any resting orders after observation.

Usage:
    python scripts/order_test.py
"""

from __future__ import annotations
import json, math, os, time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

CLOB_BASE   = "https://clob.polymarket.com"
GAMMA_BASE  = "https://gamma-api.polymarket.com"
ASSET       = "btc"
INTERVAL    = 5  # minutes


# ── Helpers ───────────────────────────────────────────────────────────────────

def _window_end_ts(offset_windows: int = 0) -> int:
    now = int(time.time())
    secs = INTERVAL * 60
    current_end = ((now + secs) // secs) * secs
    return current_end + offset_windows * secs


def _fetch_token_ids(window_end_ts: int) -> tuple[str, str] | None:
    """Return (up_token_id, down_token_id) for a given window end timestamp."""
    slug = f"{ASSET}-updown-5m-{window_end_ts}"
    r = requests.get(f"{GAMMA_BASE}/events", params={"active": "true", "slug": slug}, timeout=10)
    data = r.json()
    if not data:
        print(f"  Gamma: no event found for slug '{slug}'")
        return None
    m = data[0].get("markets", [{}])[0]
    raw = m.get("clobTokenIds", "[]")
    tokens = json.loads(raw) if isinstance(raw, str) else raw
    if len(tokens) < 2:
        print(f"  Gamma: token IDs missing for slug '{slug}'")
        return None
    print(f"  Slug : {slug}")
    print(f"  Up   : {tokens[0][:30]}...")
    print(f"  Down : {tokens[1][:30]}...")
    return tokens[0], tokens[1]


def _clob_live(token_id: str) -> bool:
    r = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=5)
    return r.status_code == 200


def _last_price(token_id: str) -> float:
    try:
        r = requests.get(f"{CLOB_BASE}/last-trade-price",
                         params={"token_id": token_id}, timeout=5)
        p = float(r.json().get("price", 0.5))
        return p if 0.01 <= p <= 0.99 else 0.5
    except Exception:
        return 0.5


def _place_order(client, token_id: str, shares: float, price: float, label: str) -> str | None:
    from py_clob_client.clob_types import OrderArgs, OrderType
    print(f"  Placing {label}: {shares} share(s) @ ${price}  (notional ${shares * price:.2f})")
    try:
        args   = OrderArgs(token_id=token_id, price=price, size=shares, side="BUY")
        signed = client.create_order(args)
        resp   = client.post_order(signed, OrderType.GTC)
        print(f"  Response: {resp}")
        oid = resp.get("orderID")
        if resp.get("success"):
            print(f"  ✅ ACCEPTED  order_id={oid}")
        else:
            print(f"  ❌ REJECTED  errorMsg={resp.get('errorMsg')}")
        return oid
    except Exception as exc:
        print(f"  ❌ EXCEPTION: {exc}")
        return None


def _cancel(client, order_id: str):
    if not order_id:
        return
    try:
        client.cancel(order_id)
        print(f"  Cancelled {order_id[:20]}...")
    except Exception as exc:
        print(f"  Cancel failed: {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    creds = ApiCreds(
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_API_SECRET"],
        api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
    )
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        creds=creds,
        signature_type=0,
    )

    print("=" * 60)
    print("TEST A — Small order: 2 shares @ $0.50 ($1.00 notional)")
    print("         Bypasses our internal 5-share minimum.")
    print("=" * 60)

    ts_current = _window_end_ts(0)
    tokens_current = _fetch_token_ids(ts_current)
    if tokens_current:
        up_token, _ = tokens_current
        live = _clob_live(up_token)
        print(f"  CLOB live: {live}")
        if live:
            price = _last_price(up_token)
            print(f"  Last price: {price}")
            # 2 shares at market price — satisfies $1 notional, but < 5 shares minimum
            shares_2 = 2.0
            oid_a = _place_order(client, up_token, shares_2, round(price, 2), "2 shares (below 5-share min)")
            time.sleep(2)
            _cancel(client, oid_a)
        else:
            print("  CLOB not live for current window — cannot test")

    print()
    print("=" * 60)
    print("TEST B — Pre-market order: next window (not yet open)")
    print("=" * 60)

    ts_next = _window_end_ts(1)
    dt_next = datetime.fromtimestamp(ts_next, tz=timezone.utc)
    print(f"  Next window ends: {dt_next.strftime('%H:%M UTC')} (end ts={ts_next})")

    tokens_next = _fetch_token_ids(ts_next)
    if tokens_next:
        up_token_next, _ = tokens_next
        live_next = _clob_live(up_token_next)
        print(f"  CLOB live for next window: {live_next}")
        price_next = _last_price(up_token_next)
        print(f"  Last price: {price_next}")
        # Try placing 5 shares (our standard size) on the next window
        oid_b = _place_order(client, up_token_next, 5.0, round(price_next, 2),
                             "5 shares on NEXT (pre-market) window")
        time.sleep(2)
        _cancel(client, oid_b)
    else:
        print("  Next window market not yet on Gamma — cannot test")

    print()
    print("Tests complete.")


if __name__ == "__main__":
    main()
