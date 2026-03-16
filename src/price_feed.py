"""
price_feed.py — Real-time and historical OHLCV price data.

Priority order:
  1. Massive.com REST (formerly Polygon.io — set POLYGON_API_KEY in .env)
  2. CCXT public endpoints (Binance, no key required)
  3. Fallback: raise UnsupportedAssetError (logged, never crashes caller)

Assets are resolved DYNAMICALLY — no hardcoded whitelist.
Any ticker that Binance or Massive knows about will work.
Unknown tickers raise UnsupportedAssetError which callers catch and log.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Dict, List, Optional

import threading

import ccxt
import pandas as pd
import requests

logger = logging.getLogger(__name__)

POLYGON_BASE    = "https://api.massive.com/v2"   # formerly api.polygon.io — same endpoint paths
_CCXT_EXCHANGE  = None   # Binance — lazy singleton
_CCXT_BYBIT     = None   # Bybit fallback — lazy singleton
_CCXT_LOCK      = threading.Lock()   # serialises load_markets() — not thread-safe in CCXT


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

def _get_bybit() -> ccxt.Exchange:
    global _CCXT_BYBIT
    if _CCXT_BYBIT is None:
        _CCXT_BYBIT = ccxt.bybit({"enableRateLimit": True})
    return _CCXT_BYBIT


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
    # Catch HTTPError and re-raise without the URL (which contains the API key)
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        raise PriceFeedError(f"Polygon HTTP {resp.status_code} for {ticker}") from None

    results = resp.json().get("results", [])
    if not results:
        raise UnsupportedAssetError(f"Polygon: no data returned for {ticker}")

    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    logger.debug("Polygon: %d bars for %s", len(df), asset)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].set_index("timestamp")


# ── CCXT / Binance ─────────────────────────────────────────────────────────────

def _fetch_from_exchange(exchange: ccxt.Exchange, asset: str, interval: str, limit: int) -> pd.DataFrame:
    """Try to fetch OHLCV from a given CCXT exchange. Raises UnsupportedAssetError if not listed."""
    name   = exchange.id
    symbol = _ccxt_symbol(asset)
    tf     = INTERVAL_TO_CCXT[interval]

    logger.debug("%s request: %s %s limit=%d", name, symbol, tf, limit)
    try:
        with _CCXT_LOCK:
            markets = exchange.load_markets()
    except Exception as exc:
        raise PriceFeedError(f"{name}: failed to load markets — {exc}") from exc

    # Try USDT, then USDC
    resolved = None
    for candidate in [symbol, f"{asset.upper()}/USDC"]:
        if candidate in markets:
            resolved = candidate
            break

    if resolved is None:
        raise UnsupportedAssetError(
            f"{name}: {symbol} (and /USDC) not listed — asset '{asset}' unavailable on {name}"
        )

    if resolved != symbol:
        logger.debug("%s: using fallback symbol %s", name, resolved)

    ohlcv = exchange.fetch_ohlcv(resolved, timeframe=tf, limit=limit)
    if not ohlcv:
        raise UnsupportedAssetError(f"{name}: empty OHLCV for {resolved}")

    df = pd.DataFrame(ohlcv, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    logger.debug("%s: %d bars for %s", name, len(df), resolved)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].set_index("timestamp")


def _fetch_ccxt(asset: str, interval: str, limit: int) -> pd.DataFrame:
    """Try Binance first, then Bybit for assets not listed on Binance (e.g. HYPE)."""
    last_exc: Exception = UnsupportedAssetError("no exchange tried")
    for exchange in [_get_ccxt_exchange(), _get_bybit()]:
        try:
            return _fetch_from_exchange(exchange, asset, interval, limit)
        except UnsupportedAssetError as exc:
            logger.debug("Not on %s: %s", exchange.id, exc)
            last_exc = exc
        except Exception as exc:
            logger.warning("%s error for %s: %s", exchange.id, asset, exc)
            last_exc = exc
    raise UnsupportedAssetError(
        f"Asset '{asset}' not found on Binance or Bybit: {last_exc}"
    )


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
        # Try Binance, then Bybit
        symbol   = _ccxt_symbol(asset)
        exchange = None
        for ex in [_get_ccxt_exchange(), _get_bybit()]:
            try:
                mkts = ex.load_markets()
                for candidate in [symbol, f"{asset.upper()}/USDC"]:
                    if candidate in mkts:
                        symbol   = candidate
                        exchange = ex
                        break
                if exchange:
                    break
            except Exception:
                continue

        if exchange is None:
            logger.warning("H1 data: '%s' not found on Binance or Bybit — skipping H1 filter", asset)
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
