"""
market_discovery.py — Fetch active 5-min and 15-min "Up or Down" markets
from the Polymarket Gamma API.

Market format on Polymarket (2026):
  "Bitcoin Up or Down - March 16, 10:45AM-10:50AM ET"
  Outcomes: ["Up", "Down"]  — clobTokenIds[0] = Up token, [1] = Down token

The CLOB API (clob.polymarket.com) only returns legacy 2022-2023 markets.
The Gamma API (gamma-api.polymarket.com) has current live markets.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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


def _match_interval(question: str, slug: str, target: str) -> bool:
    """Check if a market matches the target interval (5m or 15m)."""
    minutes = INTERVAL_MINUTES.get(target)
    if minutes is None:
        return False
    slug_lower = slug.lower()
    q_lower    = question.lower()
    # Slug check: "btc-updown-5m-..." or "btc-updown-15m-..."
    if f"-{minutes}m-" in slug_lower or f"updown-{minutes}m" in slug_lower:
        return True
    # Question check: "10:45AM-10:50AM" (5-min span) or "10:45AM-11:00AM" (15-min)
    # Fall back: just check "5m" / "15m" literal in slug
    if f"{minutes}m" in slug_lower:
        return True
    return False


# ── Gamma API ──────────────────────────────────────────────────────────────────

def _fetch_gamma_page(offset: int = 0, limit: int = 100) -> list:
    """Fetch one page of active, accepting-orders markets from the Gamma API."""
    params = {
        "active":          "true",
        "closed":          "false",
        "accepting_orders": "true",
        "limit":           limit,
        "offset":          offset,
        "order":           "endDate",
        "ascending":       "true",
    }
    logger.debug("Fetching Gamma markets offset=%d", offset)
    resp = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Public API ─────────────────────────────────────────────────────────────────

def discover_markets(
    interval:  str = "5m",
    assets:    List[str] | None = None,
    max_pages: int = 10,
) -> List[PolyMarket]:
    """
    Fetch active "Up or Down" markets from Polymarket Gamma API.
    Returns the nearest upcoming window per asset, direction agnostic
    (each PolyMarket holds both up_token_id and down_token_id).
    """
    if assets is None:
        assets = list(_ASSET_ALIASES.keys())

    assets_lower = [a.lower() for a in assets]
    found_assets: set = set()
    # Keep only the soonest market per asset (sorted by endDate ascending)
    best: Dict[str, PolyMarket] = {}

    logger.info("Starting market discovery — interval=%s assets=%s", interval, assets_lower)

    offset = 0
    limit  = 100
    pages  = 0

    while pages < max_pages:
        try:
            page_markets = _fetch_gamma_page(offset=offset, limit=limit)
        except Exception as exc:
            logger.error("Gamma API fetch error on page %d: %s", pages, exc)
            break

        if not page_markets:
            logger.debug("No more markets returned — stopping at page %d", pages)
            break

        logger.debug("Page %d: %d raw markets", pages, len(page_markets))

        for m in page_markets:
            question: str = m.get("question", "")
            slug:     str = m.get("slug", "")

            # Must be an "Up or Down" market
            if "up or down" not in question.lower():
                continue

            # Must match the requested interval
            if not _match_interval(question, slug, interval):
                logger.debug("Skipping (wrong interval): %s [%s]", question[:80], slug)
                continue

            # Must match a configured asset
            asset = _match_asset(question, assets_lower)
            if asset is None:
                continue

            # Extract token IDs from clobTokenIds (stored as JSON string)
            raw_tokens = m.get("clobTokenIds", "[]")
            try:
                token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
            except (json.JSONDecodeError, TypeError):
                logger.debug("Invalid clobTokenIds for: %s", question[:80])
                continue

            if len(token_ids) < 2:
                logger.debug("Missing token IDs for: %s", question[:80])
                continue

            pm = PolyMarket(
                condition_id=m.get("conditionId", ""),
                question=question,
                asset=asset,
                interval=interval,
                up_token_id=token_ids[0],
                down_token_id=token_ids[1],
                end_date_iso=m.get("endDate", ""),
            )

            # Keep only the soonest window per asset
            if asset not in best:
                best[asset] = pm
                found_assets.add(asset)
                logger.debug("Found market: [%s] %s (end=%s)", asset, question[:80], pm.end_date_iso)
            else:
                logger.debug("Duplicate asset %s — keeping soonest, skipping: %s", asset, question[:60])

        # If we've found all requested assets, stop paging early
        if found_assets >= set(assets_lower):
            logger.debug("All requested assets found — stopping early")
            break

        if len(page_markets) < limit:
            break  # last page

        offset += limit
        pages  += 1
        time.sleep(0.2)

    markets = list(best.values())

    # Warn about assets with no market
    missing = set(assets_lower) - found_assets
    for asset in missing:
        logger.warning(
            "Asset '%s' is in config but has NO active %s Up/Down market on Polymarket "
            "— it will be skipped this cycle.", asset, interval
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
