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
        logger.info("=" * 60)
        logger.info("BOT STARTING")
        logger.info("  mode         = %s", self.cfg.mode)
        logger.info("  interval     = %s", self.cfg.interval)
        logger.info("  assets       = %s", self.cfg.assets)
        logger.info("  progression  = %s (cap %d)", self.cfg.progression_type, self.cfg.progression_cap)
        logger.info("  hedge        = %s", self.cfg.use_hedge)
        logger.info("  kelly        = %s", self.cfg.kelly_sizing_enabled)
        logger.info("  h1_filter    = %s (threshold %.3f)", self.cfg.h1_filter_enabled, self.cfg.h1_body_threshold)
        logger.info("  rsi_filter   = %s", self.cfg.rsi_filter_enabled)
        logger.info("  parallel     = %s", self.cfg.parallel_assets)
        logger.info("  dry_run      = %s", self.cfg.dry_run)
        logger.info("=" * 60)
        alert_bot_started(self.cfg)

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

        while self._running:
            try:
                logger.debug("Hot-reloading config …")
                self.cfg = load_config()
                self._maybe_reset_daily_pnl()
                self._run_cycle()
            except Exception as exc:
                logger.exception("Unhandled cycle error: %s", exc)

            sleep_secs = INTERVAL_SECONDS.get(self.cfg.interval, 300)
            logger.info("Cycle complete — sleeping %ds until next window", sleep_secs)
            for _ in range(sleep_secs):
                if not self._running:
                    break
                time.sleep(1)

    # ── Cycle ─────────────────────────────────────────────────────────────────

    def _run_cycle(self):
        cfg = self.cfg
        now = datetime.now(timezone.utc).isoformat()
        logger.info("━" * 60)
        logger.info("CYCLE START  %s", now)
        logger.info("━" * 60)

        logger.debug("Discovering %s markets for assets: %s", cfg.interval, cfg.assets)
        markets = discover_markets(
            interval=cfg.interval,
            assets=cfg.assets,
            skip_clob_check=(cfg.weekend_behavior == "off"),
        )
        if not markets:
            logger.warning("No Polymarket markets found this cycle — nothing to trade")
            return

        market_index: Dict[str, PolyMarket] = {
            m.asset: m for m in markets
        }
        logger.info("Active markets: %d", len(markets))
        for m in markets:
            logger.debug("  Market: [%s] end=%s up_ask=%.3f down_ask=%.3f",
                         m.asset, m.end_date_iso, m.best_up_ask, m.best_down_ask)

        if cfg.mode == "live":
            logger.debug("Enriching market prices from CLOB …")
            try:
                enrich_with_prices(markets)
            except Exception as exc:
                logger.warning("Price enrichment failed: %s", exc)

        if cfg.parallel_assets:
            logger.debug("Running assets in parallel threads")
            self._run_assets_parallel(cfg, market_index)
        else:
            logger.debug("Running assets sequentially")
            for asset in cfg.assets:
                self._process_asset(cfg, asset, market_index)

        if self.executor.paper:
            dd = self._current_drawdown_pct()
            logger.info("Daily P&L: $%.2f  |  Drawdown: %.1f%%", self.executor.daily_pnl, dd)
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
        logger.debug("Processing asset: %s", asset)
        # One market per asset now covers both Up and Down tokens
        market = market_index.get(asset)
        if not market:
            logger.debug("  No active Polymarket for %s — skipping", asset)
            return

        for direction in ["up", "down"]:
            logger.debug("  Generating signal for %s/%s …", asset, direction)
            momentum_signal = direction

            with self._lock:
                try:
                    sig: TradeSignal = generate_signal(
                        asset=asset,
                        direction=direction,
                        momentum_signal=momentum_signal,
                        cfg=cfg,
                        states=self.states,
                        last_results=self.last_results,
                    )
                except Exception as exc:
                    logger.error("Signal generation error for %s/%s: %s", asset, direction, exc)
                    continue

            if sig.skipped:
                logger.info("  SKIP %s/%s — reason: %s  rsi=%.1f  h1_bias=%s",
                            asset, direction, sig.skip_reason,
                            sig.rsi_value or 0, sig.h1_bias or "none")
                continue

            logger.info("  SIGNAL %s/%s  stake=$%.2f  hedge=$%.2f  prog=%s  rsi=%.1f  h1=%s",
                        asset, direction, sig.stake_usd, sig.hedge_stake_usd,
                        sig.progression_used, sig.rsi_value or 0, sig.h1_bias or "none")

            try:
                # market has both up/down tokens; executor picks the right one by direction
                pos = self.executor.execute_signal(sig, market, market)
            except Exception as exc:
                logger.error("  Order execution error for %s/%s: %s", asset, direction, exc)
                continue

            if pos:
                logger.info("  OPENED position id=%s %s/%s $%.2f",
                            pos.main_order.order_id, asset, direction, sig.stake_usd)
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
