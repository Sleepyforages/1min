"""
complete_set.py — Complete-set arbitrage strategy for Polymarket Up/Down markets.

Based on reverse-engineered spec from ent0n29/polybot (docs/EXAMPLE_STRATEGY_SPEC.md).

Core idea:
  bid_up + bid_dn < 1.0 → buying both sides costs less than $1, and one always pays $1.
  Place GTC maker bids on BOTH sides. Wait for fills. Profit = 1 - (fill_up + fill_dn).

Timing:
  - Runs continuously every REFRESH_SECS seconds.
  - Cancels and re-quotes if price moves > REPRICE_THRESHOLD.
  - At T-60s: taker top-up the lagging leg (buy at ask).
  - At T=0: cancel all open orders for that market.

Markets:
  - Currently: SOL 5m (configurable via cfg.assets / cfg.interval).
  - Adapts to BTC/ETH 15m or 1h by changing cfg.assets.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

TICK_SIZE           = 0.01    # Polymarket minimum price increment
MIN_EDGE            = 0.01    # minimum complete-set edge to trade
IMPROVE_TICKS       = 1       # how many ticks above best_bid to quote
MAX_SKEW_TICKS      = 1       # max skew ticks for inventory imbalance
SKEW_SHARES_MAX     = 200     # imbalance shares that trigger max skew
MIN_REPLACE_SECS    = 5       # don't cancel/replace orders younger than this
MAX_ORDER_AGE_SECS  = 280     # cancel any order older than this (< 5-min window)
REFRESH_SECS        = 30      # poll interval (stay under Cloudflare rate limits)
TOPUP_SECONDS_LEFT  = 60      # seconds before end to trigger taker top-up
TOPUP_MIN_IMBALANCE = 5       # min imbalance shares to trigger top-up
WIDE_SPREAD_THRESH  = 0.20    # spread this wide → quote near mid instead

# Shares by seconds-to-end for 5m markets (adapted from spec BTC-15m table)
SHARES_TABLE_5M = [
    (30,   5),
    (60,  11),
    (120, 13),
    (180, 15),
    (300, 17),
]


# ── Per-market state ─────────────────────────────────────────────────────────

@dataclass
class MarketState:
    """Order and inventory state for one market instance."""
    # Working order IDs
    up_order_id:    str   = ""
    dn_order_id:    str   = ""
    # Prices we're currently quoting
    up_order_price: float = 0.0
    dn_order_price: float = 0.0
    # When each order was placed (unix timestamp)
    up_order_ts:    float = 0.0
    dn_order_ts:    float = 0.0
    # Filled inventory (shares)
    inv_up: float = 0.0
    inv_dn: float = 0.0
    # Prevent repeated top-up spam
    topup_done: bool = False


# ── CLOB helpers ─────────────────────────────────────────────────────────────

def get_book(token_id: str) -> Tuple[List[dict], List[dict]]:
    """Fetch bids and asks from CLOB REST. Returns (bids, asks) sorted."""
    try:
        r = requests.get("https://clob.polymarket.com/book",
                         params={"token_id": token_id}, timeout=3)
        d = r.json()
        bids = sorted(d.get("bids", []), key=lambda x: -float(x["price"]))
        asks = sorted(d.get("asks", []), key=lambda x: float(x["price"]))
        return bids, asks
    except Exception as exc:
        logger.debug("book fetch failed %s: %s", token_id[:16], exc)
        return [], []


def parse_tob(bids: List[dict], asks: List[dict]) -> Tuple[Optional[float], Optional[float]]:
    """Return (best_bid, best_ask) or None if missing."""
    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    return best_bid, best_ask


def maker_entry_price(best_bid: float, best_ask: float,
                      improve_ticks: int, skew_ticks: int) -> float:
    """
    Compute maker bid price.
    - Wide spread (>= WIDE_SPREAD_THRESH): quote near mid to avoid never-filling 0.01 traps.
    - Normal: quote at best_bid + improve_ticks + skew_ticks, capped at mid.
    """
    spread = best_ask - best_bid
    mid    = (best_bid + best_ask) / 2.0
    eff    = improve_ticks + skew_ticks

    if spread >= WIDE_SPREAD_THRESH:
        price = mid - TICK_SIZE * max(0, improve_ticks - skew_ticks)
    else:
        price = min(best_bid + TICK_SIZE * eff, mid)

    price = math.floor(price / TICK_SIZE) * TICK_SIZE  # round down to tick
    price = round(price, 3)

    # Never cross spread
    if price >= best_ask:
        price = round(best_ask - TICK_SIZE, 3)

    price = max(price, 0.01)
    return price


def shares_for_5m(seconds_to_end: float) -> float:
    """Look up share size from SHARES_TABLE_5M."""
    for threshold, size in SHARES_TABLE_5M:
        if seconds_to_end <= threshold:
            return float(size)
    return float(SHARES_TABLE_5M[-1][1])


def skew_ticks(imbalance: float) -> int:
    """Compute skew ticks from inventory imbalance."""
    scale = min(abs(imbalance) / SKEW_SHARES_MAX, 1.0)
    return round(scale * MAX_SKEW_TICKS)


# ── Engine ───────────────────────────────────────────────────────────────────

class CompleteSetEngine:
    """
    Runs the complete-set maker strategy on a set of markets.
    Call run_forever() from the bot main loop when experiment=complete_set.
    """

    def __init__(self, cfg, executor):
        self.cfg      = cfg
        self.executor = executor
        self._states:  Dict[str, MarketState] = {}  # market_id → state
        self._running = False
        self._rate_limited_until: float = 0.0  # backoff timestamp after 429

    def run_forever(self):
        """Main loop. Blocks until _running=False."""
        self._running = True
        logger.info("[cs] CompleteSetEngine starting  refresh=%ds", REFRESH_SECS)

        while self._running:
            try:
                self._tick_all()
            except Exception as exc:
                logger.exception("[cs] tick error: %s", exc)
            time.sleep(REFRESH_SECS)

    def stop(self):
        self._running = False

    # ── Internal ─────────────────────────────────────────────────────────────

    def _tick_all(self):
        """Discover active markets and tick each one."""
        from .market_discovery import discover_markets
        cfg = self.cfg

        try:
            markets = discover_markets(
                interval=cfg.interval,
                assets=cfg.assets,
                skip_clob_check=False,
                window_offset=0,   # currently-live window
            )
        except Exception as exc:
            logger.warning("[cs] market discovery failed: %s", exc)
            return

        for mkt in markets:
            try:
                self._tick_market(mkt)
            except Exception as exc:
                logger.warning("[cs] tick failed for %s: %s", mkt.asset, exc)

    def _tick_market(self, mkt):
        from datetime import datetime as DT
        state = self._states.setdefault(mkt.condition_id, MarketState())
        cfg   = self.cfg

        end_ts       = DT.fromisoformat(mkt.end_date_iso.replace("Z", "+00:00")).timestamp()
        seconds_left = end_ts - time.time()

        # Market expired — cancel everything and clean up
        if seconds_left <= 0:
            self._cancel_market(mkt, state)
            self._states.pop(mkt.condition_id, None)
            return

        # Fetch books for both legs
        bids_up, asks_up = get_book(mkt.up_token_id)
        bids_dn, asks_dn = get_book(mkt.down_token_id)

        best_bid_up, best_ask_up = parse_tob(bids_up, asks_up)
        best_bid_dn, best_ask_dn = parse_tob(bids_dn, asks_dn)

        # Stale / missing book — cancel and wait
        if best_bid_up is None or best_bid_dn is None:
            logger.debug("[cs] %s stale book — cancelling", mkt.asset)
            self._cancel_market(mkt, state)
            return

        # Complete-set edge check
        edge = 1.0 - (best_bid_up + best_bid_dn)
        logger.info("[cs] %s  up_bid=%.3f  dn_bid=%.3f  edge=%.4f  T-%.0fs",
                    mkt.asset, best_bid_up, best_bid_dn, edge, seconds_left)

        if edge < MIN_EDGE:
            logger.debug("[cs] %s edge %.4f < %.4f — cancel and wait", mkt.asset, edge, MIN_EDGE)
            self._cancel_market(mkt, state)
            return

        # Inventory skew
        imbalance = state.inv_up - state.inv_dn
        sk = skew_ticks(imbalance)
        skew_up = -sk if imbalance > 0 else +sk  # favor lagging leg
        skew_dn = +sk if imbalance > 0 else -sk

        # Maker prices
        price_up = maker_entry_price(best_bid_up, best_ask_up, IMPROVE_TICKS, skew_up)
        price_dn = maker_entry_price(best_bid_dn, best_ask_dn, IMPROVE_TICKS, skew_dn)

        # Cap per-side USD via base_bet_usd; min 5 shares (Polymarket floor)
        max_usd = getattr(cfg, "base_bet_usd", 0) or 0
        if max_usd > 0:
            size = max(5.0, math.floor(max_usd / max(price_up, price_dn)))
        else:
            size = shares_for_5m(seconds_left)
        size_usd_up = round(price_up * size, 4)
        size_usd_dn = round(price_dn * size, 4)

        logger.info("[cs] %s  quote_up=%.3f  quote_dn=%.3f  size=%.0f shares  imbalance=%.0f",
                    mkt.asset, price_up, price_dn, size, imbalance)

        # Manage UP order
        self._manage_order(mkt, state, "up", mkt.up_token_id,
                           price_up, size_usd_up, price_up)
        # Small gap between UP and DN placements helps stay under rate limit.
        if not state.dn_order_id and state.up_order_id:
            time.sleep(5)
        # Manage DOWN order
        self._manage_order(mkt, state, "dn", mkt.down_token_id,
                           price_dn, size_usd_dn, price_dn)

        # End-of-market taker top-up
        if (seconds_left <= TOPUP_SECONDS_LEFT
                and abs(imbalance) >= TOPUP_MIN_IMBALANCE
                and not state.topup_done):
            self._topup(mkt, state, imbalance, best_ask_up, best_ask_dn)

    def _manage_order(self, mkt, state: MarketState, side: str,
                      token_id: str, new_price: float, size_usd: float,
                      quoted_price: float):
        """Place new order or replace stale one."""
        order_id    = state.up_order_id    if side == "up" else state.dn_order_id
        cur_price   = state.up_order_price if side == "up" else state.dn_order_price
        order_ts    = state.up_order_ts    if side == "up" else state.dn_order_ts

        price_moved = abs(new_price - cur_price) >= TICK_SIZE
        order_old   = (time.time() - order_ts) >= MIN_REPLACE_SECS
        has_order   = bool(order_id)

        if has_order and price_moved and order_old:
            # Cancel and replace
            self._cancel_order(mkt, state, side)
            has_order = False

        if not has_order:
            oid = self._place_maker_buy(mkt, side, token_id, size_usd, new_price)
            if oid:
                if side == "up":
                    state.up_order_id    = oid
                    state.up_order_price = new_price
                    state.up_order_ts    = time.time()
                else:
                    state.dn_order_id    = oid
                    state.dn_order_price = new_price
                    state.dn_order_ts    = time.time()

    def _place_maker_buy(self, mkt, side: str, token_id: str,
                         size_usd: float, price: float) -> str:
        """Place a GTC maker buy. Returns order_id or ''."""
        cfg = self.cfg
        if cfg.mode == "paper":
            oid = f"paper_{mkt.asset}_{side}_{int(time.time())}_cs"
            logger.info("[paper-cs] GTC BUY %s/%s @ %.3f  $%.2f",
                        mkt.asset, side, price, size_usd)
            # Optimistic fill assumption for paper: mark as filled immediately
            if side == "up":
                mkt_state = self._states.get(mkt.condition_id)
                if mkt_state:
                    mkt_state.inv_up += round(size_usd / price, 2)
            else:
                mkt_state = self._states.get(mkt.condition_id)
                if mkt_state:
                    mkt_state.inv_dn += round(size_usd / price, 2)
            return oid

        # Honour rate-limit backoff
        wait = self._rate_limited_until - time.time()
        if wait > 0:
            logger.info("[cs] rate-limited — skipping %s/%s  (%.0fs remaining)",
                        mkt.asset, side, wait)
            return ""

        try:
            order_id, _, _ = self.executor.live.place_limit_buy(token_id, size_usd, price)
            logger.info("[cs] GTC BUY %s/%s  id=%s…  @ %.3f  $%.2f",
                        mkt.asset, side, order_id[:16], price, size_usd)
            return order_id
        except Exception as exc:
            if "429" in str(exc):
                self._rate_limited_until = time.time() + 120
                logger.warning("[cs] 429 rate-limit — backing off 120s  %s/%s",
                               mkt.asset, side)
            else:
                logger.error("[cs] place failed %s/%s: %s", mkt.asset, side, exc)
            return ""

    def _cancel_order(self, mkt, state: MarketState, side: str):
        order_id = state.up_order_id if side == "up" else state.dn_order_id
        if not order_id or order_id.startswith("paper_"):
            pass
        elif self.cfg.mode == "live" and self.executor.live:
            self.executor.live.cancel_order(order_id)

        if side == "up":
            state.up_order_id    = ""
            state.up_order_price = 0.0
            state.up_order_ts    = 0.0
        else:
            state.dn_order_id    = ""
            state.dn_order_price = 0.0
            state.dn_order_ts    = 0.0

    def _cancel_market(self, mkt, state: MarketState):
        if state.up_order_id:
            self._cancel_order(mkt, state, "up")
        if state.dn_order_id:
            self._cancel_order(mkt, state, "dn")

    def _topup(self, mkt, state: MarketState,
               imbalance: float, best_ask_up: float, best_ask_dn: float):
        """Taker-buy the lagging leg to balance inventory at T-60s."""
        lag_side  = "dn" if imbalance > 0 else "up"
        lag_ask   = best_ask_dn if imbalance > 0 else best_ask_up
        lag_token = mkt.down_token_id if imbalance > 0 else mkt.up_token_id
        shares    = abs(imbalance)
        size_usd  = round(shares * lag_ask, 4)

        logger.info("[cs] %s TOP-UP %s  shares=%.0f @ %.3f  $%.2f",
                    mkt.asset, lag_side, shares, lag_ask, size_usd)

        if self.cfg.mode == "paper":
            logger.info("[paper-cs] TOP-UP IOC %s/%s @ %.3f  $%.2f",
                        mkt.asset, lag_side, lag_ask, size_usd)
            state.topup_done = True
            return

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType as OT
            order_args = OrderArgs(token_id=lag_token, price=lag_ask,
                                   size=shares, side="BUY")
            signed = self.executor.live.client.create_order(order_args)
            resp   = self.executor.live.client.post_order(signed, OT.FOK)
            logger.info("[cs] TOP-UP resp: %s", resp)
            state.topup_done = True
        except Exception as exc:
            logger.error("[cs] top-up failed %s/%s: %s", mkt.asset, lag_side, exc)
