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
from .price_feed import get_rsi

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
    Applies RSI, weekend, and progression logic.
    """
    asset_key = f"{asset}_{direction}"
    state = states.setdefault(asset_key, ProgressionState())
    last_result = last_results.get(asset_key, "none")

    # ── Weekend filter ────────────────────────────────────────────────────────
    if not weekend_allows_trade(direction, momentum_signal, cfg):
        return TradeSignal(asset, direction, 0, 0, skipped=True, skip_reason="weekend")

    # ── RSI filter ────────────────────────────────────────────────────────────
    rsi_val = get_rsi(asset, period=cfg.rsi_period, interval=cfg.interval)
    if not rsi_allows_trade(asset, direction, cfg):
        return TradeSignal(asset, direction, 0, 0, rsi_value=rsi_val,
                           skipped=True, skip_reason="rsi_filter")

    # ── Base stake from progression ───────────────────────────────────────────
    raw_stake = calculate_next_bet_size(
        base_bet_usd=cfg.base_bet_usd,
        last_result=last_result,
        progression_type=cfg.progression_type,
        cap=cfg.progression_cap,
        state=state,
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
    )
