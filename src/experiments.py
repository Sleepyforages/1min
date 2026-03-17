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
    """State persisted across cycles per asset (used by experiment_3)."""
    # Tokens from the previous cycle (to check prices at next T-15s)
    prev_up_token_id:   str  = ""
    prev_down_token_id: str  = ""
    # Order IDs for fill-status checking
    prev_up_order_id:   str  = ""
    prev_down_order_id: str  = ""
    # Whether each order was immediately matched at placement time
    prev_up_matched:    bool = False
    prev_down_matched:  bool = False
    # Martingale state
    martingale_side:       Optional[str] = None  # "up" | "down" | None
    martingale_multiplier: float         = 1.0


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
    Buy both sides pre-market at $0.51. Sell the losing side after 2 minutes.
    """
    for mkt in next_markets:
        up_oid,   up_matched   = place_side(executor, mkt, "up",   cfg.base_bet_usd, EXP2_PRICE)
        down_oid, down_matched = place_side(executor, mkt, "down", cfg.base_bet_usd, EXP2_PRICE)

        logger.info("[exp2] %s — both sides placed, sell-loser in 120s", mkt.asset)
        threading.Timer(
            120,
            _sell_loser_exp2,
            args=(executor, mkt, up_oid, up_matched, down_oid, down_matched),
        ).start()


def _sell_loser_exp2(executor, mkt, up_oid, up_matched, down_oid, down_matched):
    """Called 2 min into window. Sells the lower-priced (losing) side."""
    up_p   = fetch_last_trade(mkt.up_token_id)
    down_p = fetch_last_trade(mkt.down_token_id)
    logger.info("[exp2] Sell-loser check %s  up=%.3f  down=%.3f", mkt.asset, up_p, down_p)

    if up_p <= down_p:
        loser_side, loser_token, loser_oid, loser_matched = "up",   mkt.up_token_id,   up_oid,   up_matched
    else:
        loser_side, loser_token, loser_oid, loser_matched = "down", mkt.down_token_id, down_oid, down_matched

    if executor.cfg.mode != "paper":
        filled = loser_matched or check_order_filled(loser_oid, executor.live.client)
        if not filled:
            logger.info("[exp2] %s/%s — not filled, nothing to sell", mkt.asset, loser_side)
            return
        sell_price = round(min(up_p, down_p), 2)
        try:
            executor.live.place_limit_sell(loser_token, MIN_SHARES, sell_price)
            logger.info("[exp2] %s — sold %s @ %.3f", mkt.asset, loser_side, sell_price)
        except Exception as exc:
            logger.error("[exp2] Sell failed %s/%s: %s", mkt.asset, loser_side, exc)
    else:
        logger.info("[exp2] [paper] Would sell %s/%s @ %.3f",
                    mkt.asset, loser_side, min(up_p, down_p))


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
