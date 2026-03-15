"""
backtester.py — Vectorised backtester over historical OHLCV bars.

Simulates all 4 progression methods side-by-side (or a single chosen method)
on historical data, returning per-trade records and a summary comparison table.

How it works
─────────────
1.  Fetch N bars of OHLCV for each asset.
2.  For each closed bar:
    a.  Compute RSI and apply the filter.
    b.  Decide direction: UP if close > open, DOWN otherwise.
    c.  Simulate a binary Yes bet: win if direction was correct.
    d.  Apply hedge: also bet the opposite direction; close after 50% of the bar.
3.  Run each progression method independently and record PNL, drawdown,
    max exposure.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .config import Config
from .price_feed import compute_rsi, fetch_ohlcv
from .strategy import (
    ProgressionState,
    apply_multipliers,
    calculate_next_bet_size,
)

logger = logging.getLogger(__name__)

ALL_PROGRESSION_TYPES = ["fixed", "martingale", "fibonacci", "dalembert"]


# ── Result dataclasses ─────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    timestamp: str
    asset: str
    direction: str
    progression_type: str
    stake_usd: float
    hedge_stake_usd: float
    outcome: str        # "win" | "loss"
    pnl_usd: float
    rsi: Optional[float]
    use_hedge: bool


@dataclass
class BacktestSummary:
    progression_type: str
    use_hedge: bool
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    gross_pnl: float
    net_pnl: float
    max_drawdown: float
    max_exposure: float
    sharpe: float       # simplified daily Sharpe


# ── Core simulation ────────────────────────────────────────────────────────────

def _simulate_one_method(
    df: pd.DataFrame,
    asset: str,
    cfg: Config,
    progression_type: str,
    use_hedge: bool,
) -> List[TradeRecord]:
    """Simulate one progression method on a single asset's OHLCV data."""
    records: List[TradeRecord] = []
    state = ProgressionState()
    last_result = "none"
    base = cfg.base_bet_usd

    rsi_series = compute_rsi(df["close"], cfg.rsi_period)

    for i in range(cfg.rsi_period + 1, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i - 1]
        ts = str(df.index[i])

        # ── Direction signal: simple close vs open ────────────────────────────
        direction = "up" if row["close"] > row["open"] else "down"

        # ── RSI filter ────────────────────────────────────────────────────────
        rsi_val: Optional[float] = None
        if cfg.rsi_filter_enabled and i < len(rsi_series):
            rsi_raw = rsi_series.iloc[i]
            if not pd.isna(rsi_raw):
                rsi_val = float(rsi_raw)
                if direction == "up" and rsi_val < cfg.rsi_overextended_low:
                    continue
                if direction == "down" and rsi_val > cfg.rsi_overextended_high:
                    continue

        # ── Stake from progression ────────────────────────────────────────────
        raw_stake = calculate_next_bet_size(
            base_bet_usd=base,
            last_result=last_result,
            progression_type=progression_type,
            cap=cfg.progression_cap,
            state=state,
        )
        stake = apply_multipliers(raw_stake, cfg)
        hedge_stake = stake if use_hedge else 0.0

        # ── Simulate outcome ──────────────────────────────────────────────────
        # Binary market: price went up if close > open; down otherwise.
        actual_direction = "up" if row["close"] > row["open"] else "down"
        won_main = actual_direction == direction

        # Main leg PNL (assume avg entry price = 0.50 for simplicity)
        entry_price = 0.50
        pnl = (stake / entry_price * 1.0 - stake) if won_main else -stake

        # Hedge leg: opposite direction, closed at ~50% of bar (mid-price)
        if use_hedge and hedge_stake > 0:
            hedge_direction = "down" if direction == "up" else "up"
            won_hedge = actual_direction == hedge_direction
            # Hedge sells early at price trigger (0.20) → approximate
            hedge_exit_price = cfg.hedge_sell_price_trigger
            hedge_pnl = hedge_stake * hedge_exit_price - hedge_stake
            pnl += hedge_pnl

        outcome = "win" if pnl > 0 else "loss"
        last_result = "win" if won_main else "loss"

        records.append(
            TradeRecord(
                timestamp=ts,
                asset=asset,
                direction=direction,
                progression_type=progression_type,
                stake_usd=stake,
                hedge_stake_usd=hedge_stake,
                outcome=outcome,
                pnl_usd=round(pnl, 4),
                rsi=rsi_val,
                use_hedge=use_hedge,
            )
        )

    return records


def _compute_summary(
    records: List[TradeRecord],
    progression_type: str,
    use_hedge: bool,
) -> BacktestSummary:
    if not records:
        return BacktestSummary(
            progression_type=progression_type,
            use_hedge=use_hedge,
            total_trades=0, wins=0, losses=0,
            win_rate=0, gross_pnl=0, net_pnl=0,
            max_drawdown=0, max_exposure=0, sharpe=0,
        )

    pnls = [r.pnl_usd for r in records]
    wins = sum(1 for p in pnls if p > 0)
    losses = len(pnls) - wins
    gross = sum(pnls)

    # Max drawdown (peak-to-trough on cumulative PNL)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Max single-trade exposure (main + hedge)
    max_exp = max(r.stake_usd + r.hedge_stake_usd for r in records)

    # Simplified Sharpe: mean(pnl) / std(pnl)
    import statistics
    try:
        sharpe = statistics.mean(pnls) / statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    except statistics.StatisticsError:
        sharpe = 0.0

    return BacktestSummary(
        progression_type=progression_type,
        use_hedge=use_hedge,
        total_trades=len(records),
        wins=wins,
        losses=losses,
        win_rate=round(wins / len(records) * 100, 1),
        gross_pnl=round(gross, 2),
        net_pnl=round(gross, 2),   # No commission model in this sim
        max_drawdown=round(max_dd, 2),
        max_exposure=round(max_exp, 2),
        sharpe=round(sharpe, 3),
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def run_backtest(
    cfg: Config,
    assets: Optional[List[str]] = None,
    progression_types: Optional[List[str]] = None,
    bars: int = 500,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run backtest for all combinations of (asset, progression_type, use_hedge).

    Returns
    -------
    trades_df   : every individual simulated trade as a DataFrame
    summary_df  : comparison table (one row per progression_type × hedge combo)
    """
    assets = assets or cfg.assets
    progression_types = progression_types or ALL_PROGRESSION_TYPES

    all_records: List[TradeRecord] = []

    for asset in assets:
        logger.info("Fetching %d bars for %s …", bars, asset)
        try:
            df = fetch_ohlcv(asset, interval=cfg.interval, limit=bars)
        except Exception as exc:
            logger.error("Skipping %s: %s", asset, exc)
            continue

        for ptype in progression_types:
            for use_hedge in [True, False]:
                rec = _simulate_one_method(
                    df=df,
                    asset=asset,
                    cfg=cfg,
                    progression_type=ptype,
                    use_hedge=use_hedge,
                )
                all_records.extend(rec)

    if not all_records:
        return pd.DataFrame(), pd.DataFrame()

    # ── Trades DataFrame ──────────────────────────────────────────────────────
    trades_df = pd.DataFrame([r.__dict__ for r in all_records])

    # ── Summary DataFrame ─────────────────────────────────────────────────────
    summaries: List[BacktestSummary] = []
    for ptype in progression_types:
        for use_hedge in [True, False]:
            subset = [
                r for r in all_records
                if r.progression_type == ptype and r.use_hedge == use_hedge
            ]
            summaries.append(_compute_summary(subset, ptype, use_hedge))

    summary_df = pd.DataFrame([s.__dict__ for s in summaries])
    return trades_df, summary_df


def export_trades_csv(trades_df: pd.DataFrame, path: str = "data/backtest_trades.csv"):
    """Write the trades DataFrame to CSV, ensuring progression_type column is present."""
    trades_df.to_csv(path, index=False)
    logger.info("Exported %d trades to %s", len(trades_df), path)
