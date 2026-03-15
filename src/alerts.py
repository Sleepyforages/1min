"""
alerts.py — Telegram notifications for trade events and drawdown warnings.

Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
All sends are fire-and-forget (background thread) to avoid blocking the bot loop.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(text: str) -> None:
    """Internal: POST to Telegram in a background thread."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.debug("Telegram not configured — skipping alert")
        return

    url = TELEGRAM_API.format(token=token)
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
        if not resp.ok:
            logger.warning("Telegram send failed: %s", resp.text)
    except Exception as exc:
        logger.warning("Telegram error: %s", exc)


def send_async(text: str) -> None:
    """Fire-and-forget — never blocks the trading loop."""
    threading.Thread(target=_send, args=(text,), daemon=True).start()


# ── Public alert helpers ───────────────────────────────────────────────────────

def alert_trade_win(asset: str, direction: str, pnl: float, cfg) -> None:
    if not cfg.telegram_alerts_enabled or not cfg.telegram_alert_on_win:
        return
    send_async(
        f"✅ *WIN* | `{asset.upper()}` {direction.upper()}\n"
        f"P&L: `+${pnl:.2f}`"
    )


def alert_trade_loss(asset: str, direction: str, pnl: float, cfg) -> None:
    if not cfg.telegram_alerts_enabled or not cfg.telegram_alert_on_loss:
        return
    send_async(
        f"❌ *LOSS* | `{asset.upper()}` {direction.upper()}\n"
        f"P&L: `-${abs(pnl):.2f}`"
    )


def alert_drawdown(current_dd_pct: float, cfg) -> None:
    if not cfg.telegram_alerts_enabled:
        return
    if current_dd_pct >= cfg.telegram_drawdown_alert_pct:
        send_async(
            f"⚠️ *DRAWDOWN ALERT*\n"
            f"Current drawdown: `{current_dd_pct:.1f}%` "
            f"(threshold: {cfg.telegram_drawdown_alert_pct}%)"
        )


def alert_daily_limit(daily_loss_pct: float, cfg) -> None:
    if not cfg.telegram_alerts_enabled:
        return
    send_async(
        f"🛑 *DAILY LOSS LIMIT HIT*\n"
        f"Loss: `{daily_loss_pct:.1f}%` — bot paused until midnight UTC."
    )


def alert_bot_started(cfg) -> None:
    if not cfg.telegram_alerts_enabled:
        return
    send_async(
        f"🚀 *Bot started*\n"
        f"Mode: `{cfg.mode}` | Interval: `{cfg.interval}`\n"
        f"Progression: `{cfg.progression_type}` (cap {cfg.progression_cap})\n"
        f"Hedge: `{'on' if cfg.use_hedge else 'off'}` | "
        f"Kelly: `{'on' if cfg.kelly_sizing_enabled else 'off'}`"
    )
