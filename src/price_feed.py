"""
price_feed.py — Real-time and historical OHLCV price data.

Priority order:
  1. Polygon.io REST (if POLYGON_API_KEY set)
  2. CCXT public endpoints (Binance, no key required)
  3. Fallback: raise UnsupportedAssetError (logged, never crashes caller)

Assets are resolved DYNAMICALLY — no hardcoded whitelist.
Any ticker that Binance or Polygon knows about will work.
Unknown tickers raise UnsupportedAssetError which callers catch and log.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

import ccxt
import pandas as pd
import requests

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io/v2"
_CCXT_EXCHANGE = None  # lazy singleton


# ── Custom exceptions ──────────────────────────────────────────────────────────

class PriceFeedError(RuntimeError):
    """Generic price feed failure (network, API error, etc.)"""

class UnsupportedAssetError(PriceFeedError):
    """Asset is not recognised by any price feed."""


# ── Symbol resolution (dynamic, no hardcoded whitelist) ───────────────────────

def _ccxt_symbol(asset: str) -> str:
    """Best-guess CCXT symbol for an asset. e.g. 'doge' → 'DOGE/USDT'"""
    return f"{asset.upper()}/USDT"

def _polygon_ticker(asset: str) -> str:
    """Best-guess Polygon crypto ticker. e.g. 'doge' → 'X:DOGEUSD'"""
    return f"X:{asset.upper()}USD"


INTERVAL_TO_MINUTES = {"5m": 5, "15m": 15}
INTERVAL_TO_CCXT    = {"5m": "5m", "15m": "15m"}


def _get_ccxt_exchange() -> ccxt.Exchange:
    global _CCXT_EXCHANGE
    if _CCXT_EXCHANGE is None:
        _CCXT_EXCHANGE = ccxt.binance({"enableRateLimit": True})
    return _CCXT_EXCHANGE


# ── Polygon.io ─────────────────────────────────────────────────────────────────

def _fetch_polygon(asset: str, interval: str, limit: int) -> pd.DataFrame:
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        raise PriceFeedError("POLYGON_API_KEY not set")

    ticker  = _polygon_ticker(asset)
    minutes = INTERVAL_TO_MINUTES[interval]
    from_ts = int((time.time() - limit * minutes * 60) * 1000)
    to_ts   = int(time.time() * 1000)

    url = (
        f"{POLYGON_BASE}/aggs/ticker/{ticker}/range/{minutes}/minute"
        f"/{from_ts}/{to_ts}"
    )
    logger.debug("Polygon request: %s", url)
    resp = requests.get(
        url,
        params={"adjusted": "true", "sort": "asc", "limit": limit, "apiKey": api_key},
        timeout=15,
    )

    # 404 / empty = asset not on Polygon
    if resp.status_code == 404:
        raise UnsupportedAssetError(f"Polygon: ticker {ticker} not found (404)")
    resp.raise_for_status()

    results = resp.json().get("results", [])
    if not results:
        raise UnsupportedAssetError(f"Polygon: no data returned for {ticker}")

    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    logger.debug("Polygon: %d bars for %s", len(df), asset)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].set_index("timestamp")


# ── CCXT / Binance ─────────────────────────────────────────────────────────────

def _fetch_ccxt(asset: str, interval: str, limit: int) -> pd.DataFrame:
    exchange = _get_ccxt_exchange()
    symbol   = _ccxt_symbol(asset)
    tf       = INTERVAL_TO_CCXT[interval]

    logger.debug("CCXT request: %s %s limit=%d", symbol, tf, limit)

    # Validate symbol exists on Binance before fetching
    try:
        markets = exchange.load_markets()
    except Exception as exc:
        raise PriceFeedError(f"CCXT: failed to load markets — {exc}") from exc

    if symbol not in markets:
        # Try USDC pair as fallback (e.g. some assets only have /USDC on Binance)
        usdc_symbol = f"{asset.upper()}/USDC"
        if usdc_symbol in markets:
            symbol = usdc_symbol
            logger.debug("CCXT: using fallback symbol %s", symbol)
        else:
            raise UnsupportedAssetError(
                f"CCXT: symbol {_ccxt_symbol(asset)} (and /USDC) not found on Binance — "
                f"asset '{asset}' may not be supported"
            )

    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
    if not ohlcv:
        raise UnsupportedAssetError(f"CCXT: empty OHLCV response for {symbol}")

    df = pd.DataFrame(ohlcv, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    logger.debug("CCXT: %d bars for %s", len(df), symbol)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].set_index("timestamp")


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_ohlcv(asset: str, interval: str = "5m", limit: int = 200) -> pd.DataFrame:
    """
    Return OHLCV DataFrame for any asset.
    Raises UnsupportedAssetError (logged by caller) if neither feed knows it.
    """
    asset = asset.lower()
    last_exc: Exception = PriceFeedError("no feeds attempted")

    for fetch_fn, name in [(_fetch_polygon, "Polygon"), (_fetch_ccxt, "CCXT")]:
        try:
            df = fetch_fn(asset, interval, limit)
            logger.debug("Price data for %s/%s via %s: %d bars", asset, interval, name, len(df))
            return df
        except UnsupportedAssetError as exc:
            # Asset genuinely unknown — don't try further feeds, surface immediately
            logger.warning("Unsupported asset '%s' on %s: %s", asset, name, exc)
            last_exc = exc
            continue
        except PriceFeedError as exc:
            logger.warning("%s feed error for %s: %s — trying next", name, asset, exc)
            last_exc = exc
            continue
        except Exception as exc:
            logger.warning("%s unexpected error for %s: %s — trying next", name, asset, exc)
            last_exc = exc
            continue

    raise UnsupportedAssetError(
        f"Asset '{asset}' not available on any price feed: {last_exc}"
    )


def get_latest_price(asset: str) -> Optional[float]:
    """Return latest close price, or None if asset is unsupported."""
    try:
        df = fetch_ohlcv(asset, interval="5m", limit=2)
        return float(df["close"].iloc[-1])
    except UnsupportedAssetError as exc:
        logger.error("get_latest_price: unsupported asset '%s' — %s", asset, exc)
        return None
    except Exception as exc:
        logger.error("get_latest_price failed for '%s': %s", asset, exc)
        return None


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI."""
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def get_rsi(asset: str, period: int = 14, interval: str = "5m") -> Optional[float]:
    """Return current RSI, or None on error (including unsupported asset)."""
    try:
        df         = fetch_ohlcv(asset, interval=interval, limit=period * 3)
        rsi_series = compute_rsi(df["close"], period)
        val        = rsi_series.dropna().iloc[-1]
        logger.debug("RSI(%d) for %s = %.2f", period, asset, val)
        return float(val)
    except UnsupportedAssetError as exc:
        logger.error("RSI skipped — unsupported asset '%s': %s", asset, exc)
        return None
    except Exception as exc:
        logger.error("RSI computation failed for '%s': %s", asset, exc)
        return None


def get_h1_data(asset: str) -> Optional[Dict[str, float]]:
    """
    Return current H1 candle data (open, close, body_pct, direction).
    Returns None and logs a warning for unsupported assets.
    """
    try:
        exchange = _get_ccxt_exchange()
        symbol   = _ccxt_symbol(asset)

        markets = exchange.load_markets()
        if symbol not in markets:
            usdc = f"{asset.upper()}/USDC"
            if usdc in markets:
                symbol = usdc
            else:
                logger.warning("H1 data: asset '%s' not on Binance — skipping H1 filter", asset)
                return None

        ohlcv = exchange.fetch_ohlcv(symbol, timeframe="1h", limit=2)
        if not ohlcv:
            return None

        _, h1_open, _, _, h1_close, _ = ohlcv[-1]
        body_pct  = abs(h1_close - h1_open) / h1_open if h1_open else 0.0
        direction = "up" if h1_close > h1_open else "down"
        logger.debug("H1 %s: open=%.4f close=%.4f body=%.3f%% dir=%s",
                     asset, h1_open, h1_close, body_pct * 100, direction)
        return {
            "h1_open":  float(h1_open),
            "h1_close": float(h1_close),
            "body_pct": float(body_pct),
            "direction": direction,
        }
    except Exception as exc:
        logger.warning("H1 data fetch failed for '%s': %s", asset, exc)
        return None


def validate_asset(asset: str) -> tuple[bool, str]:
    """
    Check whether an asset is supported by any price feed.
    Returns (True, "") or (False, reason_string).
    Used by the UI to validate user-entered tickers before saving config.
    """
    logger.info("Validating asset '%s' …", asset)
    try:
        fetch_ohlcv(asset.lower(), interval="5m", limit=5)
        logger.info("Asset '%s' validated OK", asset)
        return True, ""
    except UnsupportedAssetError as exc:
        reason = str(exc)
        logger.warning("Asset '%s' validation failed: %s", asset, reason)
        return False, reason
    except Exception as exc:
        reason = str(exc)
        logger.error("Asset '%s' validation error: %s", asset, reason)
        return False, reason
