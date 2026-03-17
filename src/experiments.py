"""
experiments.py — Three live trading experiments.

Shared timing contract (managed by bot._run_experiment_cycle):
  T - 30s   Wake: discover next-window markets
  T - 15s   Check last-trade prices of current window tokens
              price >= 0.80 on either side → confirmed winner
  T - 10s   Place pre-market limit orders for next window
  T + 0s    Window closes / next window opens

See docs/EXPERIMENTS.md for full definitions.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

WINNING_THRESHOLD = 0.80   # last-trade price >= this confirms a winner
EXP2_PRICE        = 0.51   # experiment_2 entry price (both sides)
EXP3_PRICE        = 0.40   # experiment_3 entry price (both sides)
MIN_SHARES        = 5.0    # Polymarket minimum


# ── Per-asset state ─────────────────────────────────────────────────────────────

@dataclass
class AssetState:
    """State persisted across cycles per asset (used by experiment_3 and experiment_4)."""
    # Tokens from the previous cycle (to check prices at next T-15s)
    prev_up_token_id:   str  = ""
    prev_down_token_id: str  = ""
    # Order IDs for fill-status checking
    prev_up_order_id:   str  = ""
    prev_down_order_id: str  = ""
    # Whether each order was immediately matched at placement time
    prev_up_matched:    bool = False
    prev_down_matched:  bool = False
    # Martingale state (experiment_3)
    martingale_side:       Optional[str] = None  # "up" | "down" | None
    martingale_multiplier: float         = 1.0
    # experiment_4 state
    current_side: str = "up"   # default first bet is UP


# ── Price utilities ──────────────────────────────────────────────────────────────

def fetch_last_trade(token_id: str) -> float:
    """Fetch last-trade price from CLOB. Returns 0.5 on failure."""
    import requests
    try:
        r = requests.get(
            "https://clob.polymarket.com/last-trade-price",
            params={"token_id": token_id},
            timeout=5,
        )
        if r.status_code == 200:
            p = r.json().get("price", "")
            if p:
                return float(p)
    except Exception:
        pass
    return 0.5


def determine_winner(up_price: float, down_price: float) -> Optional[str]:
    """Return 'up', 'down', or None if no clear winner yet."""
    if up_price >= WINNING_THRESHOLD:
        return "up"
    if down_price >= WINNING_THRESHOLD:
        return "down"
    return None


def check_markets_prices(markets) -> Dict[str, Optional[str]]:
    """
    Fetch last-trade prices for all markets and return winner per asset.
    Called at T-15s. Returns {asset: "up"|"down"|None}.
    """
    winners: Dict[str, Optional[str]] = {}
    for mkt in markets:
        up_p   = fetch_last_trade(mkt.up_token_id)
        down_p = fetch_last_trade(mkt.down_token_id)
        winner = determine_winner(up_p, down_p)
        winners[mkt.asset] = winner
        logger.info("[prices] %s  up=%.3f  down=%.3f  winner=%s",
                    mkt.asset, up_p, down_p, winner or "unclear")
    return winners


# ── Order fill check ────────────────────────────────────────────────────────────

def check_order_filled(order_id: str, clob_client) -> bool:
    """Return True if the order has been filled (matched). Defaults False on error."""
    if not order_id or order_id.startswith("paper_"):
        return True
    try:
        order = clob_client.get_order(order_id)
        status = (order.get("status") or "").lower()
        return status in ("matched", "filled")
    except Exception as exc:
        logger.debug("Fill check failed for %s…: %s", order_id[:16], exc)
        return False


# ── Order placement ─────────────────────────────────────────────────────────────

def place_side(
    executor,
    market,
    side: str,
    size_usd: float,
    price: float,
) -> Tuple[str, bool]:
    """
    Place a single pre-market limit buy.
    Returns (order_id, is_immediately_matched).
    """
    token_id = market.up_token_id if side == "up" else market.down_token_id

    if executor.cfg.mode == "paper":
        oid = f"paper_{market.asset}_{side}_{int(time.time())}"
        logger.info("[paper] %s/%s @ %.2f  $%.2f", market.asset, side, price, size_usd)
        return oid, True

    try:
        order_id, _, matched = executor.live.place_limit_buy(token_id, size_usd, price)
        logger.info("[order] %s/%s  id=%s…  matched=%s  price=%.2f  $%.2f",
                    market.asset, side, order_id[:16], matched, price, size_usd)
        return order_id, matched
    except Exception as exc:
        logger.error("[order] %s/%s failed: %s", market.asset, side, exc)
        return "", False


# ── experiment_1 ────────────────────────────────────────────────────────────────

def run_experiment_1(
    cfg,
    executor,
    prev_markets,
    next_markets,
    states: Dict[str, AssetState],
    winners: Dict[str, Optional[str]],
) -> None:
    """
    Place a single order on the winning side of the current window.

    winners: pre-computed at T-15s by bot._run_experiment_cycle.
    Only places an order if a clear winner (>= 0.80) was detected.
    """
    next_map = {m.asset: m for m in next_markets}

    for raw_asset in cfg.assets:
        asset    = raw_asset.lower()
        next_mkt = next_map.get(asset)
        winner   = winners.get(asset)

        if not next_mkt:
            logger.warning("[exp1] %s — no next market", asset)
            continue
        if winner is None:
            logger.warning("[exp1] %s — no clear winner, skipping cycle", asset)
            continue

        logger.info("[exp1] %s — placing %s @ %.2f  $%.2f",
                    asset, winner, cfg.entry_price, cfg.base_bet_usd)
        place_side(executor, next_mkt, winner, cfg.base_bet_usd, cfg.entry_price)


# ── experiment_2 ────────────────────────────────────────────────────────────────

def run_experiment_2(
    cfg,
    executor,
    next_markets,
    states: Dict[str, AssetState],
) -> None:
    """
    Buy both sides pre-market at $0.51.
    Sell the losing side with 3-attempt repricing loop:
      T+2min  GTC sell at current last-trade price
      T+3min  if unfilled: cancel → GTC sell at new price
      T+4min  if unfilled: cancel → IOC sell at new price (final, fill or die)
    """
    for mkt in next_markets:
        up_oid,   up_matched   = place_side(executor, mkt, "up",   cfg.base_bet_usd, EXP2_PRICE)
        down_oid, down_matched = place_side(executor, mkt, "down", cfg.base_bet_usd, EXP2_PRICE)

        logger.info("[exp2] %s — both sides placed, sell-loser loop starting", mkt.asset)
        threading.Timer(
            120,
            _sell_attempt_1,
            args=(executor, mkt, up_oid, up_matched, down_oid, down_matched),
        ).start()


def _pick_loser(executor, mkt, up_oid, up_matched, down_oid, down_matched):
    """
    Determine the losing side at current prices.
    Returns (loser_side, loser_token, buy_filled, up_p, down_p) or None if buy not filled.
    """
    up_p   = fetch_last_trade(mkt.up_token_id)
    down_p = fetch_last_trade(mkt.down_token_id)
    logger.info("[exp2] %s  up=%.3f  down=%.3f", mkt.asset, up_p, down_p)

    if up_p <= down_p:
        loser_side, loser_token, loser_oid, loser_matched = "up",   mkt.up_token_id,   up_oid,   up_matched
    else:
        loser_side, loser_token, loser_oid, loser_matched = "down", mkt.down_token_id, down_oid, down_matched

    if executor.cfg.mode == "live" and executor.live:
        filled = loser_matched or check_order_filled(loser_oid, executor.live.client)
        if not filled:
            logger.info("[exp2] %s/%s buy not filled — nothing to sell", mkt.asset, loser_side)
            return None

    return loser_side, loser_token, up_p, down_p


def _place_sell(executor, asset: str, side: str, token_id: str,
                price: float, order_type: str) -> str:
    """
    Place a sell order. order_type: "GTC" | "IOC".
    Returns order_id or "" on failure.
    """
    if executor.cfg.mode == "paper":
        logger.info("[paper] sell %s/%s @ %.3f  [%s]", asset, side, price, order_type)
        return f"paper_sell_{asset}_{side}"
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType as OT
        ot = OT.GTC if order_type == "GTC" else OT.FOK   # py-clob-client uses FOK for IOC behaviour
        order_args = OrderArgs(token_id=token_id, price=price, size=MIN_SHARES, side="SELL")
        signed = executor.live.client.create_order(order_args)
        resp   = executor.live.client.post_order(signed, ot)
        oid    = resp.get("orderID", "unknown")
        logger.info("[exp2] sell %s/%s  id=%s…  price=%.3f  [%s]",
                    asset, side, oid[:16], price, order_type)
        return oid
    except Exception as exc:
        logger.error("[exp2] sell failed %s/%s: %s", asset, side, exc)
        return ""


def _sell_attempt_1(executor, mkt, up_oid, up_matched, down_oid, down_matched):
    """T+2min — GTC sell at current last-trade price."""
    result = _pick_loser(executor, mkt, up_oid, up_matched, down_oid, down_matched)
    if result is None:
        return

    loser_side, loser_token, up_p, down_p = result
    sell_price = round(min(up_p, down_p), 3)
    sell_oid = _place_sell(executor, mkt.asset, loser_side, loser_token, sell_price, "GTC")

    # Pass sell state forward to attempt 2
    sell_state = {"order_id": sell_oid, "loser_side": loser_side, "loser_token": loser_token}
    threading.Timer(60, _sell_attempt_2, args=(executor, mkt, sell_state)).start()


def _sell_attempt_2(executor, mkt, sell_state: dict):
    """T+3min — if still open: cancel, repost GTC at fresh price."""
    order_id = sell_state.get("order_id", "")

    if executor.cfg.mode == "live" and executor.live and order_id:
        if check_order_filled(order_id, executor.live.client):
            logger.info("[exp2] %s — sell filled at attempt 1", mkt.asset)
            return
        executor.live.cancel_order(order_id)

    loser_token = sell_state["loser_token"]
    loser_side  = sell_state["loser_side"]
    new_price   = round(fetch_last_trade(loser_token), 3)
    sell_oid    = _place_sell(executor, mkt.asset, loser_side, loser_token, new_price, "GTC")

    sell_state["order_id"] = sell_oid
    threading.Timer(60, _sell_attempt_3, args=(executor, mkt, sell_state)).start()


def _sell_attempt_3(executor, mkt, sell_state: dict):
    """T+4min — if still open: cancel, repost IOC at fresh price (final attempt)."""
    order_id = sell_state.get("order_id", "")

    if executor.cfg.mode == "live" and executor.live and order_id:
        if check_order_filled(order_id, executor.live.client):
            logger.info("[exp2] %s — sell filled at attempt 2", mkt.asset)
            return
        executor.live.cancel_order(order_id)

    loser_token = sell_state["loser_token"]
    loser_side  = sell_state["loser_side"]
    new_price   = round(fetch_last_trade(loser_token), 3)
    _place_sell(executor, mkt.asset, loser_side, loser_token, new_price, "IOC")


# ── experiment_3 ────────────────────────────────────────────────────────────────

def run_experiment_3(
    cfg,
    executor,
    prev_markets,
    next_markets,
    states: Dict[str, AssetState],
    winners: Dict[str, Optional[str]],
) -> None:
    """
    Buy both sides pre-market at $0.40. Hold to resolution.
    Martingale ONLY when exactly one side filled AND it lost.
    """
    next_map = {m.asset: m for m in next_markets}

    for raw_asset in cfg.assets:
        asset    = raw_asset.lower()
        state    = states.setdefault(asset, AssetState())
        next_mkt = next_map.get(asset)

        if not next_mkt:
            logger.warning("[exp3] %s — no next market", asset)
            continue

        # ── Evaluate previous cycle outcome ──────────────────────────────────
        if state.prev_up_token_id and state.prev_down_token_id:
            winner = winners.get(asset)

            # Determine fill status (matched at placement, or filled since)
            up_filled   = state.prev_up_matched
            down_filled = state.prev_down_matched
            if executor.cfg.mode == "live" and executor.live:
                if not up_filled and state.prev_up_order_id:
                    up_filled = check_order_filled(state.prev_up_order_id, executor.live.client)
                if not down_filled and state.prev_down_order_id:
                    down_filled = check_order_filled(state.prev_down_order_id, executor.live.client)

            one_side_only = (up_filled != down_filled)  # XOR: exactly one filled

            logger.info("[exp3] %s prev outcome: up_filled=%s down_filled=%s winner=%s",
                        asset, up_filled, down_filled, winner or "unclear")

            if winner and one_side_only:
                filled_side = "up" if up_filled else "down"
                if filled_side != winner:
                    # One side filled AND lost → engage martingale
                    state.martingale_side = filled_side
                    state.martingale_multiplier *= 2
                    logger.info("[exp3] %s MARTINGALE on %s  ×%.0f",
                                asset, filled_side, state.martingale_multiplier)
                else:
                    _reset_martingale(state)
            else:
                _reset_martingale(state)

        # ── Compute order sizes ───────────────────────────────────────────────
        base     = cfg.base_bet_usd
        up_usd   = base * (state.martingale_multiplier if state.martingale_side == "up"   else 1.0)
        down_usd = base * (state.martingale_multiplier if state.martingale_side == "down" else 1.0)

        logger.info("[exp3] %s placing  up=$%.2f  down=$%.2f  @ %.2f",
                    asset, up_usd, down_usd, EXP3_PRICE)

        # ── Place both sides ─────────────────────────────────────────────────
        up_oid,   up_m   = place_side(executor, next_mkt, "up",   up_usd,   EXP3_PRICE)
        down_oid, down_m = place_side(executor, next_mkt, "down", down_usd, EXP3_PRICE)

        # Save state for next cycle's outcome check
        state.prev_up_token_id   = next_mkt.up_token_id
        state.prev_down_token_id = next_mkt.down_token_id
        state.prev_up_order_id   = up_oid
        state.prev_down_order_id = down_oid
        state.prev_up_matched    = up_m
        state.prev_down_matched  = down_m


def _reset_martingale(state: AssetState) -> None:
    state.martingale_side       = None
    state.martingale_multiplier = 1.0


# ── experiment_4 ────────────────────────────────────────────────────────────────

def place_side_ioc(
    executor,
    market,
    side: str,
    size_usd: float,
    price: float,
) -> Tuple[str, bool]:
    """
    Place an IOC (immediate-or-cancel) buy order.
    Returns (order_id, is_filled).
    """
    token_id = market.up_token_id if side == "up" else market.down_token_id

    if executor.cfg.mode == "paper":
        oid = f"paper_{market.asset}_{side}_{int(time.time())}_ioc"
        logger.info("[paper] IOC %s/%s @ %.3f  $%.2f", market.asset, side, price, size_usd)
        return oid, True

    try:
        import math
        from py_clob_client.clob_types import OrderArgs, OrderType as OT
        raw_shares = size_usd / price if price > 0 else MIN_SHARES
        size = max(MIN_SHARES, math.ceil(raw_shares * 1e6) / 1e6)
        order_args = OrderArgs(token_id=token_id, price=price, size=size, side="BUY")
        signed = executor.live.client.create_order(order_args)
        resp   = executor.live.client.post_order(signed, OT.FOK)
        oid    = resp.get("orderID", "unknown")
        filled = resp.get("status", "") == "matched"
        logger.info("[exp4] IOC %s/%s  id=%s…  filled=%s  price=%.3f",
                    market.asset, side, oid[:16], filled, price)
        return oid, filled
    except Exception as exc:
        logger.error("[exp4] IOC failed %s/%s: %s", market.asset, side, exc)
        return "", False


def run_experiment_4(
    cfg,
    executor,
    next_markets,
    states: Dict[str, AssetState],
    winners: Dict[str, Optional[str]],
) -> None:
    """
    IOC order at window open on the just-resolved winner.

    Timing (managed by bot._run_experiment_cycle):
      T-15s  check closing window → determine winner → update current_side
      T+0s   place IOC on current_side at window open

    First cycle default: current_side = "up".
    Price = last-trade of the winning token + 0.03 buffer (capped at 0.95).
    """
    next_map = {m.asset: m for m in next_markets}

    for raw_asset in cfg.assets:
        asset    = raw_asset.lower()
        state    = states.setdefault(asset, AssetState())
        next_mkt = next_map.get(asset)

        if not next_mkt:
            logger.warning("[exp4] %s — no next market", asset)
            continue

        # Update side from last window's winner (if confirmed)
        winner = winners.get(asset)
        if winner:
            prev_side    = state.current_side
            state.current_side = winner
            logger.info("[exp4] %s — last winner=%s  (was betting %s) → now betting %s",
                        asset, winner, prev_side, state.current_side)
        else:
            logger.info("[exp4] %s — no clear winner last window, keeping side=%s",
                        asset, state.current_side)

        # Fetch live price for IOC; add buffer so it fills
        token_id  = next_mkt.up_token_id if state.current_side == "up" else next_mkt.down_token_id
        raw_price = fetch_last_trade(token_id)
        price     = round(min(raw_price + 0.03, 0.95), 3)

        logger.info("[exp4] %s — placing IOC %s @ %.3f  $%.2f",
                    asset, state.current_side, price, cfg.base_bet_usd)
        place_side_ioc(executor, next_mkt, state.current_side, cfg.base_bet_usd, price)
