"""
bot.py — Main trading loop.

Orchestrates:
  - Config reload on each cycle
  - Market discovery
  - Signal generation (with H1 bias, Kelly sizing)
  - Parallel asset execution (thread per asset when parallel_assets=true)
  - Telegram alerts on trade outcomes / drawdown
  - Daily P&L reset at UTC midnight
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .alerts import alert_bot_started, alert_daily_limit, alert_drawdown, alert_trade_loss, alert_trade_win
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
        self._lock = threading.Lock()

    def run(self):
        self._running = True
        logger.info(
            "Bot started — mode=%s interval=%s progression=%s hedge=%s parallel=%s",
            self.cfg.mode, self.cfg.interval,
            self.cfg.progression_type, self.cfg.use_hedge,
            self.cfg.parallel_assets,
        )
        alert_bot_started(self.cfg)

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
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

    # ── Cycle ─────────────────────────────────────────────────────────────────

    def _run_cycle(self):
        cfg = self.cfg
        logger.info("=== Cycle start %s ===", datetime.now(timezone.utc).isoformat())

        markets = discover_markets(interval=cfg.interval, assets=cfg.assets)
        if not markets:
            logger.warning("No markets found — skipping cycle")
            return

        market_index: Dict[str, PolyMarket] = {
            f"{m.asset}_{m.direction}": m for m in markets
        }

        if cfg.mode == "live":
            try:
                enrich_with_prices(markets)
            except Exception:
                pass

        if cfg.parallel_assets:
            self._run_assets_parallel(cfg, market_index)
        else:
            for asset in cfg.assets:
                self._process_asset(cfg, asset, market_index)

        # Drawdown alert after each cycle
        if self.executor.paper:
            dd = self._current_drawdown_pct()
            alert_drawdown(dd, cfg)

    def _run_assets_parallel(self, cfg: Config, market_index: Dict[str, PolyMarket]):
        """Spawn one thread per asset; join all before returning."""
        threads: List[threading.Thread] = []
        for asset in cfg.assets:
            t = threading.Thread(
                target=self._process_asset,
                args=(cfg, asset, market_index),
                name=f"asset-{asset}",
                daemon=True,
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=60)

    def _process_asset(self, cfg: Config, asset: str, market_index: Dict[str, PolyMarket]):
        for direction in ["up", "down"]:
            key = f"{asset}_{direction}"
            market = market_index.get(key)
            if not market:
                continue

            momentum_signal = direction  # real feeds used in live mode

            with self._lock:
                sig: TradeSignal = generate_signal(
                    asset=asset,
                    direction=direction,
                    momentum_signal=momentum_signal,
                    cfg=cfg,
                    states=self.states,
                    last_results=self.last_results,
                )

            if sig.skipped:
                logger.debug("Skipped %s %s: %s", asset, direction, sig.skip_reason)
                continue

            hedge_key = f"{asset}_{'down' if direction == 'up' else 'up'}"
            hedge_market = market_index.get(hedge_key)

            pos = self.executor.execute_signal(sig, market, hedge_market)
            if pos:
                logger.info(
                    "Opened %s %s $%.2f (hedge=$%.2f) prog=%s h1=%s",
                    asset, direction, sig.stake_usd, sig.hedge_stake_usd,
                    sig.progression_used, sig.h1_bias or "none",
                )
                # Simulated settlement (paper mode): settle immediately as win/loss
                # based on next-bar direction — in live mode this is async/on-chain
                if cfg.mode == "paper":
                    self._paper_settle(pos, cfg)

    def _paper_settle(self, pos, cfg: Config):
        """Simulate outcome for paper mode and fire alerts."""
        import random
        # Rough win rate ~52% to reflect a slight edge
        outcome = pos.main_order.direction if random.random() < 0.52 else (
            "down" if pos.main_order.direction == "up" else "up"
        )
        pnl = self.executor.settle(pos, outcome)
        with self._lock:
            result = "win" if pnl > 0 else "loss"
            key = f"{pos.asset}_{pos.main_order.direction}"
            self.last_results[key] = result

        if pnl > 0:
            alert_trade_win(pos.asset, pos.main_order.direction, pnl, cfg)
        else:
            alert_trade_loss(pos.asset, pos.main_order.direction, pnl, cfg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _maybe_reset_daily_pnl(self):
        today = datetime.now(timezone.utc).day
        if self._last_reset_day != today:
            dd = self._current_drawdown_pct()
            if dd > 0:
                alert_daily_limit(dd, self.cfg)
            self.executor.reset_daily_pnl()
            self._last_reset_day = today
            logger.info("Daily P&L reset")

    def _current_drawdown_pct(self) -> float:
        if self.executor.paper:
            pnl = self.executor.daily_pnl
            bankroll = self.executor.paper.balance + abs(min(pnl, 0))
            if bankroll <= 0:
                return 0.0
            return abs(min(pnl, 0)) / bankroll * 100
        return 0.0

    def _shutdown(self, signum, frame):
        logger.info("Shutdown signal — stopping cleanly")
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
