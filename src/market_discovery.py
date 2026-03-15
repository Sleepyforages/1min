"""
market_discovery.py — Fetch active 5-min and 15-min "Up or Down" markets
from the Polymarket Gamma API (events endpoint).

Market format on Polymarket (2026):
  "Bitcoin Up or Down - March 16, 10:45AM-10:50AM ET"
  Outcomes: ["Up", "Down"]  — clobTokenIds[0] = Up token, [1] = Down token

Data source:
  Gamma EVENTS endpoint (gamma-api.polymarket.com/events) — has current/upcoming markets.
  Gamma MARKETS endpoint is full of orphaned Dec-2025 zombie records; DO NOT USE IT.

Window activation:
  Markets are pre-created in Gamma 12-24h before the window opens.
  CLOB only activates a market (order-book live, orders accepted) when the window starts.
  We only return markets whose window start is ≤ 15 minutes away OR already started.
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


def _keywords_for(asset: str) -> List[str]:
    asset = asset.lower()
    return _ASSET_ALIASES.get(asset, [asset])


def _match_asset(question: str, assets: List[str]) -> Optional[str]:
    q = question.lower()
    for asset in assets:
        for kw in _keywords_for(asset):
            if kw in q:
                logger.debug("Market '%s' matched asset '%s' via keyword '%s'",
                             question[:60], asset, kw)
                return asset
    return None


def _match_interval(title: str, slug: str, target: str) -> bool:
    """Check if an event matches the target interval (5m or 15m)."""
    minutes = INTERVAL_MINUTES.get(target)
    if minutes is None:
        return False
    slug_lower = slug.lower()
    # Slug pattern: "btc-updown-5m-..." or "btc-updown-15m-..."
    if f"-{minutes}m-" in slug_lower or f"updown-{minutes}m" in slug_lower:
        return True
    # Fallback: check literal in slug
    if f"{minutes}m" in slug_lower:
        return True
    return False


# ── Gamma API (events endpoint) ────────────────────────────────────────────────

def _fetch_gamma_events_page(offset: int = 0, limit: int = 100) -> list:
    """Fetch one page of active events from the Gamma events endpoint.

    The events endpoint (not /markets) has real current/upcoming intraday markets.
    Sorted by endDate ascending so the nearest windows come first.
    """
    params = {
        "active":    "true",
        "closed":    "false",
        "limit":     limit,
        "offset":    offset,
        "order":     "endDate",
        "ascending": "true",
    }
    logger.debug("Fetching Gamma events offset=%d", offset)
    resp = requests.get(f"{GAMMA_BASE}/events", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Public API ─────────────────────────────────────────────────────────────────

def discover_markets(
    interval:  str = "5m",
    assets:    List[str] | None = None,
    max_pages: int = 5,
) -> List[PolyMarket]:
    """
    Fetch active "Up or Down" markets from the Polymarket Gamma events API.

    Only returns markets whose trading window is currently open OR starts
    within the next 15 minutes (CLOB activates order books at window open).

    Returns the nearest upcoming window per asset, direction agnostic
    (each PolyMarket holds both up_token_id and down_token_id).
    """
    if assets is None:
        assets = list(_ASSET_ALIASES.keys())

    assets_lower = [a.lower() for a in assets]
    found_assets: set = set()
    # Keep only the soonest market per asset (sorted by endDate ascending)
    best: Dict[str, PolyMarket] = {}

    interval_mins = INTERVAL_MINUTES.get(interval)
    if interval_mins is None:
        logger.error("Unknown interval '%s'", interval)
        return []

    logger.info("Starting market discovery — interval=%s assets=%s", interval, assets_lower)

    offset = 0
    limit  = 100
    pages  = 0

    while pages < max_pages:
        try:
            page_events = _fetch_gamma_events_page(offset=offset, limit=limit)
        except Exception as exc:
            logger.error("Gamma events API fetch error on page %d: %s", pages, exc)
            break

        if not page_events:
            logger.debug("No more events returned — stopping at page %d", pages)
            break

        logger.debug("Page %d: %d raw events", pages, len(page_events))

        now = datetime.now(timezone.utc)

        for event in page_events:
            title: str = event.get("title", "")
            slug:  str = event.get("slug",  "")

            # Must be an "Up or Down" market
            if "up or down" not in title.lower():
                continue

            # Must match the requested interval
            if not _match_interval(title, slug, interval):
                logger.debug("Skipping (wrong interval): %s [%s]", title[:80], slug)
                continue

            # Pull the embedded market record
            markets_list = event.get("markets", [])
            if not markets_list:
                logger.debug("Event has no embedded markets: %s", title[:80])
                continue
            m = markets_list[0]

            # Compute trading window start/end
            # endDate in the market is the window END; start = end - interval
            end_str = m.get("endDate", "") or event.get("endDate", "")
            if not end_str:
                logger.debug("No endDate for event: %s", title[:80])
                continue

            try:
                end_dt     = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                start_dt   = end_dt - timedelta(minutes=interval_mins)

                # Only trade windows that are currently active OR open within next 15 min.
                # "opens_soon"  = window hasn't started yet but is ≤15 min away.
                # "active"      = window has started and hasn't ended yet.
                # Past windows (start < now AND end < now) are excluded.
                window_opens_soon = now <= start_dt <= now + timedelta(minutes=15)
                window_active     = start_dt <= now <= end_dt

                if not (window_opens_soon or window_active):
                    logger.debug(
                        "Skipping (window not imminent: starts %s, now %s): %s",
                        start_dt.strftime("%H:%M"), now.strftime("%H:%M"), title[:80],
                    )
                    continue
            except (ValueError, AttributeError) as exc:
                logger.debug("Cannot parse endDate '%s' for %s: %s", end_str, title[:60], exc)
                continue

            # Must match a configured asset
            asset = _match_asset(title, assets_lower)
            if asset is None:
                continue

            # Extract token IDs from clobTokenIds (JSON string inside the market record)
            raw_tokens = m.get("clobTokenIds", "[]")
            try:
                token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            except (json.JSONDecodeError, TypeError):
                logger.debug("Invalid clobTokenIds for: %s", title[:80])
                continue

            if len(token_ids) < 2:
                logger.debug("Missing token IDs for: %s", title[:80])
                continue

            pm = PolyMarket(
                condition_id=m.get("conditionId", ""),
                question=title,
                asset=asset,
                interval=interval,
                up_token_id=token_ids[0],
                down_token_id=token_ids[1],
                end_date_iso=end_str,
                window_start_iso=start_dt.isoformat(),
            )

            # Keep only the soonest window per asset (events are sorted endDate asc)
            if asset not in best:
                best[asset] = pm
                found_assets.add(asset)
                logger.info(
                    "Found market: [%s] %s  window=%s–%s UTC",
                    asset, title[:60],
                    start_dt.strftime("%H:%M"), end_dt.strftime("%H:%M"),
                )
            else:
                logger.debug("Duplicate asset %s — keeping soonest, skipping: %s", asset, title[:60])

        # Stop early if all requested assets found
        if found_assets >= set(assets_lower):
            logger.debug("All requested assets found — stopping early")
            break

        if len(page_events) < limit:
            break  # last page

        offset += limit
        pages  += 1
        time.sleep(0.2)

    markets = list(best.values())

    missing = set(assets_lower) - found_assets
    for asset in missing:
        logger.warning(
            "Asset '%s' has NO active %s Up/Down market opening within 15 min — "
            "skipping this cycle.", asset, interval
        )

    logger.info(
        "Discovery complete: %d markets found across %d/%d requested assets",
        len(markets), len(found_assets), len(assets_lower),
    )
    return markets


def enrich_with_prices(markets: List[PolyMarket], clob_client=None) -> List[PolyMarket]:
    """Enrich markets with live ask prices from the CLOB order book."""
    if clob_client is None:
        logger.debug("enrich_with_prices: no clob_client, skipping")
        return markets
    for m in markets:
        try:
            ob = clob_client.get_order_book(m.up_token_id)
            asks = ob.get("asks", [])
            if asks:
                m.best_up_ask = float(asks[0]["price"])
            ob2 = clob_client.get_order_book(m.down_token_id)
            asks2 = ob2.get("asks", [])
            if asks2:
                m.best_down_ask = float(asks2[0]["price"])
            logger.debug("Prices enriched for %s: up=%.3f down=%.3f",
                         m.asset, m.best_up_ask, m.best_down_ask)
        except Exception as exc:
            logger.warning("Price enrichment failed for %s: %s", m.condition_id, exc)
    return markets
