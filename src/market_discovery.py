"""
market_discovery.py — Fetch active 5-min and 15-min "Up or Down" markets
from the Polymarket Gamma API using direct slug-based lookup.

Market format on Polymarket (2026):
  "Bitcoin Up or Down - March 16, 10:45AM-10:50AM ET"
  Outcomes: ["Up", "Down"]  — clobTokenIds[0] = Up token, [1] = Down token

How it works:
  5-minute markets follow a slug pattern: "{asset}-updown-5m-{UNIX_TS}"
  where UNIX_TS is the exact Unix timestamp of the window END time.
  Window end timestamps align to 5-min boundaries (:00, :05, ..., :55).
  15-minute boundaries: :00, :15, :30, :45.

  We compute the end timestamp of the CURRENT window and fetch each asset
  by slug directly — no pagination, no scanning through thousands of events.

  Final gate: check the CLOB order book for each token.  The CLOB only
  activates a market when the window actually opens (on weekdays during US
  trading hours).  On weekends or outside trading hours all token order books
  return 404 — we skip those markets rather than firing orders that will fail.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

INTERVAL_MINUTES = {
    "5m":  5,
    "15m": 15,
}

# Well-known aliases kept for accuracy; everything else falls back to the ticker itself
_ASSET_ALIASES: Dict[str, List[str]] = {
    "btc":  ["bitcoin", "btc"],
    "eth":  ["ethereum", "eth"],
    "sol":  ["solana", "sol"],
    "xrp":  ["xrp", "ripple"],
    "doge": ["dogecoin", "doge"],
    "bnb":  ["bnb", "binance coin", "binancecoin"],
    "hype": ["hyperliquid", "hype"],
    "ada":  ["cardano", "ada"],
    "avax": ["avalanche", "avax"],
    "dot":  ["polkadot", "dot"],
    "link": ["chainlink", "link"],
    "matic":["polygon", "matic"],
    "ltc":  ["litecoin", "ltc"],
    "atom": ["cosmos", "atom"],
}


@dataclass
class PolyMarket:
    condition_id:  str
    question:      str
    asset:         str   # as given in config, e.g. "btc", "doge"
    interval:      str   # "5m" | "15m"
    up_token_id:   str   # clobTokenIds[0]
    down_token_id: str   # clobTokenIds[1]
    end_date_iso:  str
    window_start_iso: str = ""   # computed: end - interval
    best_up_ask:   float = 0.0
    best_down_ask: float = 0.0

    # Convenience aliases expected by executor/strategy
    @property
    def yes_token_id(self) -> str:
        return self.up_token_id

    @property
    def no_token_id(self) -> str:
        return self.down_token_id


# ── Window timestamp helpers ────────────────────────────────────────────────────

def _current_window_end_ts(interval_mins: int) -> int:
    """Return the Unix timestamp of the END of the currently active window.

    5-minute windows end at :00, :05, :10, ..., :55 past each hour.
    15-minute windows end at :00, :15, :30, :45.
    """
    now_ts = int(time.time())
    interval_secs = interval_mins * 60
    # Ceiling division: smallest multiple of interval_secs that is > now_ts
    return ((now_ts + interval_secs) // interval_secs) * interval_secs


# ── Gamma event lookup by slug ─────────────────────────────────────────────────

def _fetch_event_by_slug(slug: str) -> Optional[dict]:
    """Fetch a single Gamma event by its exact slug.  Returns None if not found."""
    try:
        resp = requests.get(
            f"{GAMMA_BASE}/events",
            params={"active": "true", "slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list):
            return data[0]
        return None
    except Exception as exc:
        logger.debug("Gamma slug lookup failed for '%s': %s", slug, exc)
        return None


# ── CLOB liveness check ────────────────────────────────────────────────────────

def _is_clob_live(token_id: str) -> bool:
    """Return True if the CLOB order book for this token is accessible (HTTP 200).

    Uses the /book endpoint (not /order-book which is legacy/unused).
    Returns False only for genuinely missing markets, not for empty books.
    """
    try:
        resp = requests.get(
            f"{CLOB_BASE}/book",
            params={"token_id": token_id},
            timeout=5,
        )
        return resp.status_code == 200
    except Exception as exc:
        logger.debug("CLOB liveness check failed for %s…: %s", token_id[:20], exc)
        return False


# ── Public API ─────────────────────────────────────────────────────────────────

def discover_markets(
    interval:  str = "5m",
    assets:    List[str] | None = None,
    max_pages: int = 5,   # kept for API compat, not used in slug approach
    skip_clob_check: bool = False,
) -> List[PolyMarket]:
    """
    Discover active "Up or Down" markets for the CURRENT trading window.

    Strategy:
      1. Compute the current window end timestamp (5-min or 15-min aligned).
      2. Fetch each asset's market directly by slug:
           "{asset}-updown-{interval}-{window_end_unix_ts}"
      3. Verify the CLOB order book is live (HTTP 200).  If not → skip.
         (CLOB returns 404 on weekends and for pre-created future windows.)

    Returns one PolyMarket per asset that is both Gamma-present and CLOB-live.
    """
    if assets is None:
        assets = list(_ASSET_ALIASES.keys())

    assets_lower = [a.lower() for a in assets]

    interval_mins = INTERVAL_MINUTES.get(interval)
    if interval_mins is None:
        logger.error("Unknown interval '%s'", interval)
        return []

    # Compute current window
    window_end_ts    = _current_window_end_ts(interval_mins)
    window_start_ts  = window_end_ts - interval_mins * 60
    window_end_dt    = datetime.fromtimestamp(window_end_ts,   tz=timezone.utc)
    window_start_dt  = datetime.fromtimestamp(window_start_ts, tz=timezone.utc)

    logger.info(
        "Market discovery — interval=%s  window=%s–%s UTC  assets=%s",
        interval,
        window_start_dt.strftime("%H:%M"),
        window_end_dt.strftime("%H:%M"),
        assets_lower,
    )

    markets: List[PolyMarket] = []

    for asset in assets_lower:
        slug = f"{asset}-updown-{interval}-{window_end_ts}"
        logger.debug("Looking up slug: %s", slug)

        event = _fetch_event_by_slug(slug)
        if event is None:
            logger.warning(
                "Asset '%s': no Gamma event found for slug '%s' — "
                "this asset may not have a %s Up/Down market today.",
                asset, slug, interval,
            )
            continue

        mkt_list = event.get("markets", [])
        if not mkt_list:
            logger.debug("Event '%s' has no embedded markets", slug)
            continue
        m = mkt_list[0]

        # Extract token IDs
        raw_tokens = m.get("clobTokenIds", "[]")
        try:
            token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid clobTokenIds for '%s'", slug)
            continue

        if len(token_ids) < 2:
            logger.warning("Missing token IDs for '%s'", slug)
            continue

        # CLOB liveness check — confirms the market is live and accepting orders.
        # Skipped when weekend_behavior="off".
        if not skip_clob_check and not _is_clob_live(token_ids[0]):
            logger.warning(
                "Asset '%s': CLOB not live for window %s–%s UTC. Skipping.",
                asset,
                window_start_dt.strftime("%H:%M"),
                window_end_dt.strftime("%H:%M"),
            )
            continue

        pm = PolyMarket(
            condition_id=m.get("conditionId", ""),
            question=event.get("title", m.get("question", "")),
            asset=asset,
            interval=interval,
            up_token_id=token_ids[0],
            down_token_id=token_ids[1],
            end_date_iso=m.get("endDate", "") or event.get("endDate", ""),
            window_start_iso=window_start_dt.isoformat(),
        )
        markets.append(pm)
        logger.info(
            "Found live market: [%s] %s  window=%s–%s UTC",
            asset, pm.question[:60],
            window_start_dt.strftime("%H:%M"),
            window_end_dt.strftime("%H:%M"),
        )

    logger.info(
        "Discovery complete: %d CLOB-live markets / %d requested assets",
        len(markets), len(assets_lower),
    )
    return markets


def _get_best_ask(token_id: str) -> float:
    """Fetch best ask price from CLOB /book endpoint. Returns 0.0 on failure."""
    try:
        resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=5)
        if resp.status_code == 200:
            asks = resp.json().get("asks", [])
            if asks:
                return float(asks[0]["price"])
    except Exception as exc:
        logger.debug("Best ask fetch failed for %s…: %s", token_id[:20], exc)
    return 0.0


def enrich_with_prices(markets: List[PolyMarket], clob_client=None) -> List[PolyMarket]:
    """Enrich markets with live ask prices from the CLOB /book endpoint."""
    for m in markets:
        try:
            m.best_up_ask   = _get_best_ask(m.up_token_id)
            m.best_down_ask = _get_best_ask(m.down_token_id)
            logger.debug("Prices for %s: up_ask=%.3f down_ask=%.3f",
                         m.asset, m.best_up_ask, m.best_down_ask)
        except Exception as exc:
            logger.warning("Price enrichment failed for %s: %s", m.condition_id, exc)
    return markets
