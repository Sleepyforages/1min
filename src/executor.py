"""
executor.py — Order placement, hedge management, and position tracking.

In paper mode all orders are simulated locally; no network calls are made.
In live mode orders go through the Polymarket CLOB via py-clob-client.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class Order:
    order_id: str
    asset: str
    direction: str      # "up" | "down"
    token_id: str
    side: str           # "buy" | "sell"
    size_usd: float
    price: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    filled: bool = False
    fill_price: float = 0.0
    is_hedge: bool = False


@dataclass
class Position:
    asset: str
    direction: str
    main_order: Order
    hedge_order: Optional[Order] = None
    hedge_closed: bool = False
    resolved: bool = False
    pnl_usd: float = 0.0


# ── Paper ledger ───────────────────────────────────────────────────────────────

class PaperLedger:
    """Simulates fills and tracks P&L without touching real money."""

    def __init__(self, starting_balance: float = 1000.0):
        self.balance = starting_balance
        self.trades: List[dict] = []
        self._lock = threading.Lock()

    def place_order(self, order: Order) -> str:
        """Simulate immediate fill at the requested price."""
        order_id = f"paper_{int(time.time()*1000)}_{order.asset}_{order.direction}"
        with self._lock:
            self.balance -= order.size_usd
            order.filled = True
            order.fill_price = order.price
            order.order_id = order_id
            self.trades.append({
                "order_id": order_id,
                "asset": order.asset,
                "direction": order.direction,
                "side": order.side,
                "size_usd": order.size_usd,
                "price": order.price,
                "timestamp": order.timestamp.isoformat(),
                "is_hedge": order.is_hedge,
            })
        logger.info(
            "[PAPER] %s %s %s @ %.3f — balance: $%.2f",
            order.side.upper(), order.asset, order.direction, order.price, self.balance,
        )
        return order_id

    def settle_position(self, pos: Position, outcome: str) -> float:
        """
        Settle a position given the market outcome.
        outcome: "up" | "down"
        Returns net P&L in USD.
        """
        main = pos.main_order
        pnl = 0.0

        # Main leg
        if outcome == main.direction:
            # Win: payout at ~$1.00 per share (binary yes/no)
            pnl += main.size_usd / main.fill_price * 1.0 - main.size_usd
        else:
            pnl -= main.size_usd

        # Hedge leg (opposite direction — sold early or settled)
        if pos.hedge_order and not pos.hedge_closed:
            hedge = pos.hedge_order
            if outcome == hedge.direction:
                pnl += hedge.size_usd / hedge.fill_price * 1.0 - hedge.size_usd
            else:
                pnl -= hedge.size_usd

        with self._lock:
            self.balance += pnl + main.size_usd
            if pos.hedge_order and not pos.hedge_closed:
                self.balance += pos.hedge_order.size_usd
        pos.pnl_usd = pnl
        pos.resolved = True
        logger.info(
            "[PAPER] Settle %s %s outcome=%s PNL=%.2f balance=%.2f",
            main.asset, main.direction, outcome, pnl, self.balance,
        )
        return pnl


# ── Live executor ──────────────────────────────────────────────────────────────

class LiveExecutor:
    """Wraps py-clob-client for real order placement."""

    def __init__(self, cfg):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        )
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,  # Polygon mainnet
            key=cfg.private_key,
            creds=creds,
        )
        logger.info("LiveExecutor initialised")

    def place_market_buy(self, token_id: str, size_usd: float, price: float) -> str:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=size_usd,
            price=price,
        )
        resp = self.client.create_market_order(order_args)
        order_id = resp.get("orderID", "unknown")
        logger.info("Live order placed: %s", order_id)
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            return True
        except Exception as exc:
            logger.error("Cancel failed for %s: %s", order_id, exc)
            return False


# ── Unified Executor (routes paper / live) ────────────────────────────────────

class Executor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.positions: List[Position] = []
        self.daily_pnl: float = 0.0
        self._lock = threading.Lock()

        if cfg.mode == "paper":
            self.paper = PaperLedger()
            self.live: Optional[LiveExecutor] = None
        else:
            self.paper = None
            self.live = LiveExecutor(cfg)

    # ── Public methods ─────────────────────────────────────────────────────────

    def execute_signal(
        self,
        signal,
        market,  # PolyMarket
        hedge_market=None,  # PolyMarket for opposite direction
    ) -> Optional[Position]:
        if signal.skipped:
            return None
        if self._daily_loss_exceeded():
            logger.warning("Daily loss limit hit — skipping trade")
            return None
        if self.cfg.dry_run:
            logger.info("[DRY-RUN] Would trade %s %s $%.2f", signal.asset, signal.direction, signal.stake_usd)
            return None

        # One market holds both Up and Down tokens — pick by signal direction
        main_token = market.up_token_id   if signal.direction == "up"   else market.down_token_id
        main_price = market.best_up_ask   if signal.direction == "up"   else market.best_down_ask

        main_order = self._place_order(
            asset=signal.asset,
            direction=signal.direction,
            token_id=main_token,
            size_usd=signal.stake_usd,
            price=main_price or 0.5,
            is_hedge=False,
        )

        hedge_order = None
        if self.cfg.use_hedge and signal.hedge_stake_usd > 0 and hedge_market:
            hedge_direction = "down" if signal.direction == "up" else "up"
            hedge_token = hedge_market.down_token_id if signal.direction == "up" else hedge_market.up_token_id
            hedge_price = hedge_market.best_down_ask if signal.direction == "up" else hedge_market.best_up_ask
            hedge_order = self._place_order(
                asset=signal.asset,
                direction=hedge_direction,
                token_id=hedge_token,
                size_usd=signal.hedge_stake_usd,
                price=hedge_price or 0.5,
                is_hedge=True,
            )
            # Schedule hedge close after trigger time
            threading.Timer(
                self.cfg.hedge_sell_trigger_minutes * 60,
                self._close_hedge,
                args=(hedge_order,),
            ).start()

        pos = Position(
            asset=signal.asset,
            direction=signal.direction,
            main_order=main_order,
            hedge_order=hedge_order,
        )
        with self._lock:
            self.positions.append(pos)
        return pos

    def settle(self, pos: Position, outcome: str) -> float:
        if self.cfg.mode == "paper":
            pnl = self.paper.settle_position(pos, outcome)
        else:
            pnl = 0.0  # live settlement is async (market resolves on-chain)
        with self._lock:
            self.daily_pnl += pnl
        return pnl

    def reset_daily_pnl(self):
        with self._lock:
            self.daily_pnl = 0.0

    # ── Private helpers ────────────────────────────────────────────────────────

    def _place_order(
        self, asset, direction, token_id, size_usd, price, is_hedge
    ) -> Order:
        order = Order(
            order_id="",
            asset=asset,
            direction=direction,
            token_id=token_id,
            side="buy",
            size_usd=size_usd,
            price=price,
            is_hedge=is_hedge,
        )
        if self.cfg.mode == "paper":
            self.paper.place_order(order)
        else:
            order.order_id = self.live.place_market_buy(token_id, size_usd, price)
            order.filled = True
        return order

    def _close_hedge(self, hedge_order: Order):
        """Called by timer to exit the hedge leg early."""
        logger.info("Closing hedge leg %s", hedge_order.order_id)
        if self.cfg.mode != "paper" and self.live:
            self.live.cancel_order(hedge_order.order_id)
        # Mark in the parent position
        for pos in self.positions:
            if pos.hedge_order and pos.hedge_order.order_id == hedge_order.order_id:
                pos.hedge_closed = True
                break

    def _daily_loss_exceeded(self) -> bool:
        if self.cfg.mode == "paper" and self.paper:
            total = self.paper.balance + abs(self.daily_pnl)
            if total == 0:
                return False
            loss_pct = abs(min(self.daily_pnl, 0)) / total * 100
            return loss_pct >= self.cfg.max_daily_loss_pct
        return False
