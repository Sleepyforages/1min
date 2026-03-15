"""
strategy.py — Bet-sizing, progression logic, and signal generation.

Progression methods
───────────────────
fixed       : always return base_bet_usd (no progression)
martingale  : double stake on loss; reset to base on win;
              capped at `cap` doublings → max = base * 2^cap
fibonacci   : walk forward one step in [1,1,2,3,5,8,13,21,...] * base on loss;
              walk back two steps on win; floor = 0 (step 0)
dalembert   : add 1 unit on loss; subtract 1 unit on win;
              floor = base_bet_usd; ceil = base * cap

All methods respect the US-hours multiplier transparently via
`apply_multipliers()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .config import Config
from .price_feed import get_h1_data, get_rsi

logger = logging.getLogger(__name__)

# Standard Fibonacci sequence (index = step, value = multiplier)
FIBONACCI_SEQ: List[int] = [1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144]


# ── Per-asset progression state ───────────────────────────────────────────────

@dataclass
class ProgressionState:
    """Tracks progression position for one asset."""
    streak_losses: int = 0          # martingale: consecutive losses
    fib_step: int = 0               # fibonacci: current step index
    dalembert_units: float = 0.0    # dalembert: extra units above base
    # H1 bias tracking
    h1_bias_direction: Optional[str] = None   # "up" | "down" | None
    h1_bias_trades_left: int = 0              # trades remaining in the bias window


def _reset_state(state: ProgressionState) -> ProgressionState:
    state.streak_losses = 0
    state.fib_step = 0
    state.dalembert_units = 0.0
    return state


# ── Core sizing function ───────────────────────────────────────────────────────

def calculate_next_bet_size(
    base_bet_usd: float,
    last_result: str,           # "win" | "loss" | "push" | "none"
    progression_type: str,
    cap: int,
    state: ProgressionState,
) -> float:
    """
    Return the next stake in USD and mutate `state` in-place.

    Parameters
    ----------
    base_bet_usd    : configured base stake
    last_result     : outcome of the previous trade ("none" on first trade)
    progression_type: one of fixed | martingale | fibonacci | dalembert
    cap             : maximum progression steps (3–7)
    state           : mutable per-asset state object

    Returns
    -------
    float : next bet size in USD (always >= base_bet_usd)
    """
    # Push / none → treat same as win (no penalty)
    won = last_result in ("win", "push", "none")

    if progression_type == "fixed":
        _reset_state(state)
        return base_bet_usd

    elif progression_type == "martingale":
        # ── Update state ──────────────────────────────────────────────────────
        if won:
            state.streak_losses = 0
        else:
            state.streak_losses = min(state.streak_losses + 1, cap)

        # 2^losses doublings, capped at 2^cap
        multiplier = 2 ** state.streak_losses
        return base_bet_usd * multiplier

    elif progression_type == "fibonacci":
        # ── Update state ──────────────────────────────────────────────────────
        if won:
            state.fib_step = max(0, state.fib_step - 2)
        else:
            state.fib_step = min(state.fib_step + 1, cap)

        # Extend sequence if cap > len(FIBONACCI_SEQ)
        seq = _fibonacci_up_to(cap + 1)
        return base_bet_usd * seq[state.fib_step]

    elif progression_type == "dalembert":
        # ── Update state ──────────────────────────────────────────────────────
        if won:
            state.dalembert_units = max(0.0, state.dalembert_units - 1.0)
        else:
            state.dalembert_units = min(state.dalembert_units + 1.0, cap - 1)

        return base_bet_usd * (1.0 + state.dalembert_units)

    else:
        logger.warning("Unknown progression_type '%s', defaulting to fixed", progression_type)
        return base_bet_usd


def _fibonacci_up_to(n: int) -> List[int]:
    """Return Fibonacci sequence of length n (minimum 12 elements)."""
    seq = [1, 1]
    while len(seq) < max(n, 12):
        seq.append(seq[-1] + seq[-2])
    return seq


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def kelly_bet_size(cfg: Config) -> float:
    """
    Half-Kelly criterion:
        f = (edge / odds) * kelly_fraction
        stake = f * bankroll
        capped at kelly_max_bet_pct % of bankroll

    For binary markets where payout is ~$1 per $0.50 stake (odds ≈ 1.0):
        f = edge * kelly_fraction
    """
    if not cfg.kelly_sizing_enabled:
        return cfg.base_bet_usd

    edge = cfg.kelly_estimated_edge          # e.g. 0.04
    fraction = cfg.kelly_fraction            # e.g. 0.5  (half-Kelly)
    bankroll = cfg.kelly_bankroll_usd

    # Binary market assumed at 50/50 fair odds → odds = 1.0
    f = edge * fraction
    stake = round(f * bankroll, 2)

    # Hard cap
    max_stake = round(bankroll * cfg.kelly_max_bet_pct / 100, 2)
    stake = min(stake, max_stake)

    # Floor = base_bet_usd
    return max(stake, cfg.base_bet_usd)


# ── H1 momentum filter ────────────────────────────────────────────────────────

def h1_get_bias(asset: str, cfg: Config, state: ProgressionState) -> Optional[str]:
    """
    Check the current H1 candle.  If body_pct > threshold, set a directional
    bias for the next `h1_bias_duration_trades` trades and return that direction.
    Returns the active bias direction (may be from a previous call), or None.
    """
    if not cfg.h1_filter_enabled:
        return None

    # Refresh bias from live H1 data
    h1 = get_h1_data(asset)
    if h1 and h1["body_pct"] >= cfg.h1_body_threshold:
        new_dir = h1["direction"]
        if new_dir != state.h1_bias_direction:
            # New H1 bias window starts
            state.h1_bias_direction = new_dir
            state.h1_bias_trades_left = cfg.h1_bias_duration_trades
            logger.info(
                "H1 bias triggered for %s: %s (body=%.2f%%)",
                asset, new_dir, h1["body_pct"] * 100,
            )

    # Count down bias window
    if state.h1_bias_trades_left > 0:
        state.h1_bias_trades_left -= 1
        return state.h1_bias_direction

    # Bias expired → reset
    state.h1_bias_direction = None
    return None


# ── Multiplier layer ───────────────────────────────────────────────────────────

def apply_multipliers(stake: float, cfg: Config) -> float:
    """Apply US-hours multiplier if we're within the configured window."""
    now_utc = datetime.now(timezone.utc)
    hour = now_utc.hour
    if cfg.us_hours_start_utc <= hour < cfg.us_hours_end_utc:
        stake *= cfg.us_hours_multiplier
    return round(stake, 2)


# ── RSI filter ────────────────────────────────────────────────────────────────

def rsi_allows_trade(asset: str, direction: str, cfg: Config) -> bool:
    """
    Return False if RSI is overextended in the direction we want to bet.
    UP bets need RSI >= rsi_overextended_low (market not already overextended up)
    DOWN bets need RSI <= rsi_overextended_high
    """
    if not cfg.rsi_filter_enabled:
        return True
    rsi = get_rsi(asset, period=cfg.rsi_period, interval=cfg.interval)
    if rsi is None:
        return True  # fail open
    if direction == "up" and rsi < cfg.rsi_overextended_low:
        logger.info("RSI %.1f < %.1f — skipping UP bet on %s", rsi, cfg.rsi_overextended_low, asset)
        return False
    if direction == "down" and rsi > cfg.rsi_overextended_high:
        logger.info("RSI %.1f > %.1f — skipping DOWN bet on %s", rsi, cfg.rsi_overextended_high, asset)
        return False
    return True


# ── Weekend filter ────────────────────────────────────────────────────────────

def weekend_allows_trade(direction: str, momentum_signal: Optional[str], cfg: Config) -> bool:
    """
    Returns False on weekends if behavior == 'skip'.
    Returns True only when direction matches momentum_signal if 'momentum_only'.
    """
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() < 5:  # Mon–Fri
        return True
    # Saturday / Sunday
    if cfg.weekend_behavior == "skip":
        return False
    if cfg.weekend_behavior == "momentum_only":
        return momentum_signal == direction
    return True


# ── Signal generation ─────────────────────────────────────────────────────────

@dataclass
class TradeSignal:
    asset: str
    direction: str          # "up" | "down"
    stake_usd: float
    hedge_stake_usd: float  # 0 if use_hedge=False
    rsi_value: Optional[float] = None
    h1_bias: Optional[str] = None       # active H1 bias direction, if any
    progression_used: str = ""          # which progression was actually applied
    skipped: bool = False
    skip_reason: str = ""


def generate_signal(
    asset: str,
    direction: str,
    momentum_signal: Optional[str],
    cfg: Config,
    states: Dict[str, ProgressionState],
    last_results: Dict[str, str],
) -> TradeSignal:
    """
    Produce a TradeSignal for one (asset, direction) pair.
    Applies H1 bias, RSI, weekend, Kelly sizing, and progression logic.
    """
    # Use a shared per-asset state key (not per-direction) for H1 bias
    asset_state_key = f"{asset}_state"
    asset_state = states.setdefault(asset_state_key, ProgressionState())

    asset_key = f"{asset}_{direction}"
    dir_state = states.setdefault(asset_key, ProgressionState())
    last_result = last_results.get(asset_key, "none")

    # ── Weekend filter ────────────────────────────────────────────────────────
    if not weekend_allows_trade(direction, momentum_signal, cfg):
        return TradeSignal(asset, direction, 0, 0, skipped=True, skip_reason="weekend")

    # ── H1 momentum filter ────────────────────────────────────────────────────
    h1_bias = h1_get_bias(asset, cfg, asset_state)
    if h1_bias is not None and h1_bias != direction:
        # H1 candle is strongly trending the other way — skip this direction
        return TradeSignal(asset, direction, 0, 0, h1_bias=h1_bias,
                           skipped=True, skip_reason="h1_counter_trend")

    # ── RSI filter ────────────────────────────────────────────────────────────
    rsi_val = get_rsi(asset, period=cfg.rsi_period, interval=cfg.interval)
    if not rsi_allows_trade(asset, direction, cfg):
        return TradeSignal(asset, direction, 0, 0, rsi_value=rsi_val,
                           h1_bias=h1_bias, skipped=True, skip_reason="rsi_filter")

    # ── Determine active progression (H1 bias can force override) ────────────
    active_progression = cfg.progression_type
    if h1_bias == direction and cfg.h1_filter_enabled:
        active_progression = cfg.h1_force_progression
        logger.debug("H1 bias active for %s %s → forcing %s", asset, direction, active_progression)

    # ── Base stake: Kelly or fixed/progressive ────────────────────────────────
    base = kelly_bet_size(cfg)   # returns cfg.base_bet_usd if Kelly disabled

    raw_stake = calculate_next_bet_size(
        base_bet_usd=base,
        last_result=last_result,
        progression_type=active_progression,
        cap=cfg.progression_cap,
        state=dir_state,
    )
    stake = apply_multipliers(raw_stake, cfg)

    # ── Hedge leg ─────────────────────────────────────────────────────────────
    hedge_stake = stake if cfg.use_hedge else 0.0

    return TradeSignal(
        asset=asset,
        direction=direction,
        stake_usd=stake,
        hedge_stake_usd=hedge_stake,
        rsi_value=rsi_val,
        h1_bias=h1_bias,
        progression_used=active_progression,
    )
