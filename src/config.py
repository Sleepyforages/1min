"""
config.py — Load, validate, and hot-reload the YAML configuration.

The Config object is a thin dataclass wrapper around the YAML file.
The UI writes back to the file; the bot re-reads it on each cycle.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal

import yaml
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config/default.yaml"))

ProgressionType = Literal["fixed", "martingale", "fibonacci", "dalembert"]
IntervalType = Literal["5m", "15m"]
ModeType = Literal["live", "paper", "backtest"]
WeekendBehavior = Literal["skip", "momentum_only", "off"]


@dataclass
class Config:
    # Operating mode
    mode: ModeType = "paper"

    # Market
    interval: IntervalType = "5m"
    assets: List[str] = field(default_factory=lambda: ["btc", "eth", "sol", "xrp"])

    # Bet sizing
    base_bet_usd: float = 1.0

    # Progression
    progression_type: ProgressionType = "fixed"
    progression_cap: int = 7

    # Hedge
    use_hedge: bool = True
    hedge_sell_trigger_minutes: float = 2.5
    hedge_sell_price_trigger: float = 0.20

    # US hours
    us_hours_multiplier: float = 2.0
    us_hours_start_utc: int = 14
    us_hours_end_utc: int = 20

    # RSI filter
    rsi_filter_enabled: bool = True
    rsi_period: int = 14
    rsi_overextended_low: float = 45.0
    rsi_overextended_high: float = 55.0

    # H1 momentum filter
    h1_filter_enabled: bool = True
    h1_body_threshold: float = 0.003
    h1_bias_duration_trades: int = 12
    h1_force_progression: str = "martingale"

    # Kelly sizing
    kelly_sizing_enabled: bool = False
    kelly_fraction: float = 0.5
    kelly_bankroll_usd: float = 100.0
    kelly_estimated_edge: float = 0.04
    kelly_max_bet_pct: float = 5.0

    # Telegram alerts
    telegram_alerts_enabled: bool = False
    telegram_alert_on_win: bool = False
    telegram_alert_on_loss: bool = True
    telegram_drawdown_alert_pct: float = 5.0

    # Parallel execution
    parallel_assets: bool = True

    # Weekend
    weekend_behavior: WeekendBehavior = "momentum_only"

    # Risk
    dry_run: bool = False
    max_daily_loss_pct: float = 10.0

    # Logging
    log_level: str = "INFO"

    # ── Credentials (never from YAML) ─────────────────────────────────────────
    @property
    def private_key(self) -> str:
        return os.environ["POLYMARKET_PRIVATE_KEY"]

    @property
    def api_key(self) -> str:
        return os.getenv("POLYMARKET_API_KEY", "")

    @property
    def api_secret(self) -> str:
        return os.getenv("POLYMARKET_API_SECRET", "")

    @property
    def api_passphrase(self) -> str:
        return os.getenv("POLYMARKET_API_PASSPHRASE", "")

    @property
    def funder_address(self) -> str:
        """
        Main wallet address (the one that holds USDC).
        Set POLYMARKET_FUNDER_ADDRESS in .env if the private key is for a
        proxy/operator wallet rather than the main EOA.
        Leave unset if private key IS the main wallet.
        """
        return os.getenv("POLYMARKET_FUNDER_ADDRESS", "")


def load_config(path: Path = CONFIG_PATH) -> Config:
    """Read YAML and return a Config dataclass. Falls back to defaults on missing keys."""
    if not path.exists():
        return Config()
    with open(path) as f:
        raw: dict = yaml.safe_load(f) or {}

    # Filter only known fields to avoid __init__ errors
    known = {k: v for k, v in raw.items() if k in Config.__dataclass_fields__}
    return Config(**known)


def save_config(cfg: Config, path: Path = CONFIG_PATH) -> None:
    """Persist a Config back to YAML (credentials excluded)."""
    skip = {"private_key", "api_key", "api_secret", "api_passphrase"}
    data = {
        k: getattr(cfg, k)
        for k in Config.__dataclass_fields__
        if k not in skip
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
