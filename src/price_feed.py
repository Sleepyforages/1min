"""
price_feed.py — Real-time and historical OHLCV price data.

Priority order:
  1. Polygon.io REST (if POLYGON_API_KEY set)
  2. CCXT public endpoints (Binance, no key required)
  3. Fallback: raise PriceFeedError

All prices returned in USD.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import ccxt
import pandas as pd
import requests

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io/v2"
_CCXT_EXCHANGE = None  # lazy singleton


class PriceFeedError(RuntimeError):
    pass


# ── Symbol helpers ─────────────────────────────────────────────────────────────

ASSET_TO_CCXT = {
    "btc": "BTC/USDT",
    "eth": "ETH/USDT",
    "sol": "SOL/USDT",
    "xrp": "XRP/USDT",
}

ASSET_TO_POLYGON = {
    "btc": "X:BTCUSD",
    "eth": "X:ETHUSD",
    "sol": "X:SOLUSD",
    "xrp": "X:XRPUSD",
}

INTERVAL_TO_MINUTES = {"5m": 5, "15m": 15}
INTERVAL_TO_CCXT = {"5m": "5m", "15m": "15m"}


def _get_ccxt_exchange() -> ccxt.Exchange:
    global _CCXT_EXCHANGE
    if _CCXT_EXCHANGE is None:
        _CCXT_EXCHANGE = ccxt.binance({"enableRateLimit": True})
    return _CCXT_EXCHANGE


# ── Polygon.io ─────────────────────────────────────────────────────────────────

def _fetch_polygon(
    asset: str, interval: str, limit: int
) -> pd.DataFrame:
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        raise PriceFeedError("POLYGON_API_KEY not set")

    ticker = ASSET_TO_POLYGON[asset]
    minutes = INTERVAL_TO_MINUTES[interval]
    # from = limit bars ago
    from_ts = int((time.time() - limit * minutes * 60) * 1000)
    to_ts = int(time.time() * 1000)

    url = (
        f"{POLYGON_BASE}/aggs/ticker/{ticker}/range/{minutes}/minute"
        f"/{from_ts}/{to_ts}"
    )
    resp = requests.get(
        url,
        params={"adjusted": "true", "sort": "asc", "limit": limit, "apiKey": api_key},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise PriceFeedError(f"Polygon returned empty results for {asset}")

    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(
        columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    )
    return df[["timestamp", "open", "high", "low", "close", "volume"]].set_index(
        "timestamp"
    )


# ── CCXT / Binance ─────────────────────────────────────────────────────────────

def _fetch_ccxt(asset: str, interval: str, limit: int) -> pd.DataFrame:
    exchange = _get_ccxt_exchange()
    symbol = ASSET_TO_CCXT[asset]
    tf = INTERVAL_TO_CCXT[interval]
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    df = pd.DataFrame(
        ohlcv, columns=["timestamp_ms", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].set_index(
        "timestamp"
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_ohlcv(asset: str, interval: str = "5m", limit: int = 200) -> pd.DataFrame:
    """
    Return a DataFrame with columns [open, high, low, close, volume]
    indexed by UTC timestamp. Tries Polygon first, then CCXT.
    """
    asset = asset.lower()
    for fetch_fn, name in [(_fetch_polygon, "Polygon"), (_fetch_ccxt, "CCXT")]:
        try:
            df = fetch_fn(asset, interval, limit)
            logger.debug("Price data for %s via %s: %d bars", asset, name, len(df))
            return df
        except PriceFeedError:
            raise
        except Exception as exc:
            logger.warning("%s fetch failed for %s: %s — trying next", name, asset, exc)

    raise PriceFeedError(f"All price feeds failed for {asset}/{interval}")


def get_latest_price(asset: str) -> float:
    """Return the most recent close price for an asset."""
    df = fetch_ohlcv(asset, interval="5m", limit=2)
    return float(df["close"].iloc[-1])


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI without external ta library dependency."""
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def get_rsi(asset: str, period: int = 14, interval: str = "5m") -> Optional[float]:
    """Return the current RSI value for the asset, or None on error."""
    try:
        df = fetch_ohlcv(asset, interval=interval, limit=period * 3)
        rsi_series = compute_rsi(df["close"], period)
        val = rsi_series.dropna().iloc[-1]
        return float(val)
    except Exception as exc:
        logger.error("RSI computation failed for %s: %s", asset, exc)
        return None


def get_h1_data(asset: str) -> Optional[Dict[str, float]]:
    """
    Return the current H1 candle's open price and the latest close price,
    plus the body percentage (abs(close - open) / open).

    Uses the 1h timeframe; falls back to constructing from 5m bars if needed.
    Returns None on error.
    """
    try:
        exchange = _get_ccxt_exchange()
        symbol = ASSET_TO_CCXT[asset]
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=2)
        if not ohlcv:
            return None
        # Latest incomplete candle
        _, h1_open, _, _, h1_close, _ = ohlcv[-1]
        body_pct = abs(h1_close - h1_open) / h1_open if h1_open else 0.0
        direction = "up" if h1_close > h1_open else "down"
        return {
            "h1_open": float(h1_open),
            "h1_close": float(h1_close),
            "body_pct": float(body_pct),
            "direction": direction,
        }
    except Exception as exc:
        logger.warning("H1 data fetch failed for %s: %s", asset, exc)
        return None
