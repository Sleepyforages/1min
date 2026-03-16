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

from .alerts import alert_bot_started, alert_daily_limit, alert_drawdown, alert_redemption, alert_trade_loss, alert_trade_win
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
        self._cycle_count: int = 0

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

        if self.cfg.mode == "live" and not self.cfg.dry_run:
            self._live_preflight()

        while self._running:
            try:
                logger.debug("Hot-reloading config …")
                self.cfg = load_config()
                self._maybe_reset_daily_pnl()
                self._run_cycle()
                self._cycle_count += 1
                # Auto-redeem every 3 cycles (~15 min at 5m interval)
                if self.cfg.mode == "live" and self._cycle_count % 3 == 0:
                    self._auto_redeem()
            except Exception as exc:
                logger.exception("Unhandled cycle error: %s", exc)

            # Sleep until 2 seconds AFTER the next window boundary so we always
            # enter at the very start of a fresh window (not mid-window).
            interval_secs = INTERVAL_SECONDS.get(self.cfg.interval, 300)
            now_ts = int(time.time())
            next_boundary = ((now_ts + interval_secs) // interval_secs) * interval_secs
            sleep_secs = max(1, next_boundary - now_ts + 2)
            logger.info("Cycle complete — sleeping %ds until next window boundary", sleep_secs)
            for _ in range(sleep_secs):
                if not self._running:
                    break
                time.sleep(1)

    # ── Cycle ─────────────────────────────────────────────────────────────────

    def _run_cycle(self):
        cfg = self.cfg
        # Sync executor with freshly hot-reloaded config so dry_run, use_hedge,
        # max_daily_loss_pct, etc. are never stale inside the executor
        self.executor.cfg = cfg
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

    def _get_signal_direction(self, asset: str, cfg: Config) -> Optional[str]:
        """
        Determine ONE direction for this asset this cycle from the last closed price bar.
        Returns "up" if close > open, "down" otherwise, or None if data unavailable.
        """
        try:
            from .price_feed import fetch_ohlcv
            df = fetch_ohlcv(asset, interval=cfg.interval, limit=3)
            if len(df) < 2:
                return None
            last = df.iloc[-2]  # last CLOSED bar (not the still-open current one)
            direction = "up" if last["close"] > last["open"] else "down"
            logger.debug("  Direction signal %s: %s (close=%.4f open=%.4f)",
                         asset, direction, last["close"], last["open"])
            return direction
        except Exception as exc:
            logger.warning("  Direction fetch failed for %s: %s — skipping", asset, exc)
            return None

    def _process_asset(self, cfg: Config, asset: str, market_index: Dict[str, PolyMarket]):
        logger.debug("Processing asset: %s", asset)
        market = market_index.get(asset)
        if not market:
            logger.debug("  No active Polymarket for %s — skipping", asset)
            return

        # Select ONE direction per asset per cycle — never trade both sides simultaneously
        direction = self._get_signal_direction(asset, cfg)
        if direction is None:
            logger.info("  %s — no direction signal available, skipping", asset)
            return

        logger.debug("  Generating signal for %s/%s …", asset, direction)
        with self._lock:
            try:
                sig: TradeSignal = generate_signal(
                    asset=asset,
                    direction=direction,
                    momentum_signal=direction,
                    cfg=cfg,
                    states=self.states,
                    last_results=self.last_results,
                )
            except Exception as exc:
                logger.error("Signal generation error for %s/%s: %s", asset, direction, exc)
                return

        if sig.skipped:
            logger.info("  SKIP %s/%s — reason: %s  rsi=%.1f  h1_bias=%s",
                        asset, direction, sig.skip_reason,
                        sig.rsi_value or 0, sig.h1_bias or "none")
            return

        logger.info("  SIGNAL %s/%s  stake=$%.2f  hedge=$%.2f  prog=%s  rsi=%.1f  h1=%s",
                    asset, direction, sig.stake_usd, sig.hedge_stake_usd,
                    sig.progression_used, sig.rsi_value or 0, sig.h1_bias or "none")

        try:
            # market has both up/down tokens; executor picks the right one by direction
            pos = self.executor.execute_signal(sig, market, market)
        except Exception as exc:
            logger.error("  Order execution error for %s/%s: %s", asset, direction, exc)
            return

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
            self._append_paper_trade(pos, outcome, pnl)

        if pnl > 0:
            alert_trade_win(pos.asset, pos.main_order.direction, pnl, cfg)
        else:
            alert_trade_loss(pos.asset, pos.main_order.direction, pnl, cfg)

    def _append_paper_trade(self, pos, outcome: str, pnl: float):
        """Persist paper trade to data/paper_trades.csv for the Live Monitor."""
        import csv
        from pathlib import Path
        path = Path("data/paper_trades.csv")
        path.parent.mkdir(exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        balance = self.executor.paper.balance if self.executor.paper else 0.0
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["timestamp", "asset", "direction", "stake_usd",
                             "outcome", "pnl_usd", "balance"])
            w.writerow([
                pos.main_order.timestamp.isoformat(),
                pos.asset,
                pos.main_order.direction,
                round(pos.main_order.size_usd, 4),
                outcome,
                round(pnl, 4),
                round(balance, 4),
            ])

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

    def _live_preflight(self):
        """
        Validate live trading readiness before entering the cycle loop.
        Checks CLOB auth, USDC.e balance, and exchange allowances.
        Logs warnings rather than hard-failing so the operator can react via the UI.
        """
        import requests as _req
        logger.info("Running live preflight checks…")
        live = self.executor.live
        if live is None:
            logger.error("Preflight: executor has no live client — cannot validate")
            return

        # 1. CLOB auth
        try:
            orders = live.client.get_orders()
            logger.info("Preflight OK: CLOB auth  open_orders=%d", len(orders) if orders else 0)
        except Exception as exc:
            logger.error("Preflight FAIL: CLOB auth error — %s", exc)
            logger.error("Bot will continue but live orders will fail until auth is fixed")
            return

        # 2. CLOB balance and allowances
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, RequestArgs
            from py_clob_client.endpoints import GET_BALANCE_ALLOWANCE
            from py_clob_client.headers.headers import create_level_2_headers
            from py_clob_client.http_helpers.helpers import add_balance_allowance_params_to_url
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
            req = RequestArgs(method="GET", request_path=GET_BALANCE_ALLOWANCE)
            hdrs = create_level_2_headers(live.client.signer, live.client.creds, req)
            url = add_balance_allowance_params_to_url(
                "https://clob.polymarket.com" + GET_BALANCE_ALLOWANCE, params)
            r = _req.get(url, headers=hdrs, timeout=10)
            if r.status_code == 200:
                data = r.json()
                balance = int(data.get("balance", 0)) / 1e6
                allowances = data.get("allowances", {})
                any_allowance = any(int(v) > 0 for v in allowances.values())
                logger.info("Preflight OK: CLOB balance=$%.4f  allowances_set=%s",
                            balance, any_allowance)
                if balance < 1.0:
                    logger.warning("Preflight WARNING: CLOB balance $%.4f < $1 — "
                                   "trades will likely fail. Add USDC.e to wallet.", balance)
                if not any_allowance:
                    logger.error("Preflight FAIL: no USDC.e allowances set — "
                                 "run the approval script before starting live trading")
        except Exception as exc:
            logger.warning("Preflight: balance check error (non-fatal): %s", exc)

        logger.info("Preflight complete — entering cycle loop")

    def _auto_redeem(self):
        """
        Redeem all redeemable winning positions on-chain.
        Called every 3 cycles (~15 min). Fires a Telegram alert with the amount.
        """
        import os
        from web3 import Web3
        from web3.middleware import ExtraDataToPOAMiddleware

        WALLET         = "0x347C80CE3a2786AE2e7f2BcE57f64aD032904A63"
        CTF_CONTRACT   = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        USDC_E         = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        PARENT_COLL_ID = b"\x00" * 32
        RPC_URL        = "https://polygon-bor-rpc.publicnode.com"
        CTF_ABI        = [{
            "name": "redeemPositions", "type": "function",
            "inputs": [
                {"name": "collateralToken",    "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId",        "type": "bytes32"},
                {"name": "indexSets",          "type": "uint256[]"},
            ],
            "outputs": [], "stateMutability": "nonpayable",
        }]

        try:
            import requests as _req
            r = _req.get("https://data-api.polymarket.com/positions",
                         params={"user": WALLET, "sizeThreshold": "0.01"}, timeout=15)
            r.raise_for_status()
            positions = r.json()
        except Exception as exc:
            logger.warning("Auto-redeem: positions fetch failed — %s", exc)
            return

        from collections import defaultdict
        by_cond: dict = defaultdict(list)
        total_redeemable_usd = 0.0
        for p in positions:
            if p.get("redeemable") and p.get("curPrice", 0) == 1.0:
                cid = p["conditionId"]
                idx = 2 ** p["outcomeIndex"]
                if idx not in by_cond[cid]:
                    by_cond[cid].append(idx)
                total_redeemable_usd += p.get("currentValue", 0)

        if not by_cond:
            logger.debug("Auto-redeem: nothing to redeem this cycle")
            return

        logger.info("Auto-redeem: %d conditions, est. $%.2f", len(by_cond), total_redeemable_usd)

        private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
        if not private_key:
            logger.error("Auto-redeem: POLYMARKET_PRIVATE_KEY not set")
            return

        try:
            w3 = Web3(Web3.HTTPProvider(RPC_URL))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            account = w3.eth.account.from_key(private_key)
            ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_CONTRACT), abi=CTF_ABI)

            success = 0
            for cid, index_sets in by_cond.items():
                try:
                    nonce     = w3.eth.get_transaction_count(account.address, "latest")
                    gas_price = int(w3.eth.gas_price * 2)
                    tx = ctf.functions.redeemPositions(
                        Web3.to_checksum_address(USDC_E),
                        PARENT_COLL_ID,
                        bytes.fromhex(cid.removeprefix("0x")),
                        index_sets,
                    ).build_transaction({"from": account.address, "nonce": nonce,
                                         "gasPrice": gas_price, "gas": 120_000})
                    signed = account.sign_transaction(tx)
                    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                    if receipt["status"] == 1:
                        success += 1
                        logger.info("Auto-redeem OK: %s  block=%d", cid[:20], receipt["blockNumber"])
                    else:
                        logger.warning("Auto-redeem reverted: %s", cid[:20])
                    import time as _time; _time.sleep(3)
                except Exception as exc:
                    logger.warning("Auto-redeem tx error for %s: %s", cid[:20], exc)

            if success > 0:
                # Fetch new wallet balance
                try:
                    bal_r = _req.post(RPC_URL, json={
                        "jsonrpc": "2.0", "method": "eth_call",
                        "params": [{"to": USDC_E,
                                    "data": "0x70a08231" + WALLET[2:].zfill(64)}, "latest"],
                        "id": 1,
                    }, timeout=8)
                    wallet_bal = int(bal_r.json().get("result", "0x0"), 16) / 1e6
                except Exception:
                    wallet_bal = 0.0

                logger.info("Auto-redeem complete: %d/%d redeemed  $%.2f  wallet=$%.2f",
                            success, len(by_cond), total_redeemable_usd, wallet_bal)
                alert_redemption(success, total_redeemable_usd, wallet_bal, self.cfg)

        except Exception as exc:
            logger.error("Auto-redeem failed: %s", exc)

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

    bot = Bot(cfg)
    bot.run()


if __name__ == "__main__":
    main()
