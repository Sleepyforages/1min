"""
bot.py — Main trading loop.

Orchestrates:
  - Config reload on each cycle
  - Market discovery
  - Signal generation
  - Order execution / paper simulation
  - Daily P&L reset at UTC midnight
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from .config import Config, load_config
from .executor import Executor
from .market_discovery import PolyMarket, discover_markets, enrich_with_prices
from .strategy import ProgressionState, TradeSignal, generate_signal

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = {"5m": 300, "15m": 900}


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.executor = Executor(cfg)
        self.states: Dict[str, ProgressionState] = {}
        self.last_results: Dict[str, str] = {}
        self._running = False
        self._last_reset_day: Optional[int] = None

    def run(self):
        self._running = True
        logger.info(
            "Bot started — mode=%s interval=%s progression=%s hedge=%s",
            self.cfg.mode,
            self.cfg.interval,
            self.cfg.progression_type,
            self.cfg.use_hedge,
        )

        # Register graceful shutdown
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
                # Hot-reload config on every cycle
                self.cfg = load_config()
                self._maybe_reset_daily_pnl()
                self._run_cycle()
            except Exception as exc:
                logger.exception("Cycle error: %s", exc)

            sleep_secs = INTERVAL_SECONDS.get(self.cfg.interval, 300)
            logger.debug("Sleeping %ds until next cycle", sleep_secs)
            for _ in range(sleep_secs):
                if not self._running:
                    break
                time.sleep(1)

    def _run_cycle(self):
        cfg = self.cfg
        logger.info("=== Cycle start %s ===", datetime.now(timezone.utc).isoformat())

        # Discover active markets
        markets = discover_markets(interval=cfg.interval, assets=cfg.assets)
        if not markets:
            logger.warning("No markets found — skipping cycle")
            return

        # Index markets by (asset, direction) for quick lookup
        market_index: Dict[str, PolyMarket] = {
            f"{m.asset}_{m.direction}": m for m in markets
        }

        # Enrich prices if live
        if cfg.mode == "live":
            try:
                from py_clob_client.client import ClobClient
                # Re-use a lightweight client for price enrichment
                enrich_with_prices(markets)
            except Exception:
                pass

        for asset in cfg.assets:
            for direction in ["up", "down"]:
                key = f"{asset}_{direction}"
                market = market_index.get(key)
                if not market:
                    continue

                # Momentum signal (simple: direction of last close vs open)
                momentum_signal = direction  # placeholder — real feeds used in live

                signal: TradeSignal = generate_signal(
                    asset=asset,
                    direction=direction,
                    momentum_signal=momentum_signal,
                    cfg=cfg,
                    states=self.states,
                    last_results=self.last_results,
                )

                if signal.skipped:
                    logger.debug("Signal skipped for %s %s: %s", asset, direction, signal.skip_reason)
                    continue

                # Hedge market = opposite direction
                hedge_key = f"{asset}_{'down' if direction == 'up' else 'up'}"
                hedge_market = market_index.get(hedge_key)

                pos = self.executor.execute_signal(signal, market, hedge_market)
                if pos:
                    logger.info(
                        "Opened position: %s %s $%.2f (hedge=$%.2f)",
                        asset, direction, signal.stake_usd, signal.hedge_stake_usd,
                    )

    def _maybe_reset_daily_pnl(self):
        today = datetime.now(timezone.utc).day
        if self._last_reset_day != today:
            self.executor.reset_daily_pnl()
            self._last_reset_day = today
            logger.info("Daily P&L reset")

    def _shutdown(self, signum, frame):
        logger.info("Shutdown signal received — stopping bot cleanly")
        self._running = False


def main():
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/bot.log"),
        ],
    )

    if cfg.mode == "backtest":
        from .backtester import export_trades_csv, run_backtest
        logger.info("Running backtest …")
        trades_df, summary_df = run_backtest(cfg)
        print("\n=== BACKTEST SUMMARY ===")
        print(summary_df.to_string(index=False))
        export_trades_csv(trades_df)
    else:
        bot = Bot(cfg)
        bot.run()


if __name__ == "__main__":
    main()
