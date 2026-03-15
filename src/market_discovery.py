"""
market_discovery.py — Fetch all active 5-min and 15-min Up/Down markets from
the Polymarket CLOB API.

Assets are resolved DYNAMICALLY from config — no hardcoded whitelist.
If a ticker has no Polymarket market, it is skipped with a WARNING log
(never crashes the bot).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"

INTERVAL_KEYWORDS = {
    "5m":  ["5-minute", "5 minute", "5min", "next 5"],
    "15m": ["15-minute", "15 minute", "15min", "next 15"],
}

# Direction keywords in market question titles
_UP_KEYWORDS   = ["up", "higher", "above", "rise", "bull"]
_DOWN_KEYWORDS = ["down", "lower", "below", "fall", "bear", "drop"]


@dataclass
class PolyMarket:
    condition_id: str
    question:     str
    asset:        str   # as given in config, e.g. "btc", "doge"
    direction:    str   # "up" | "down"
    interval:     str   # "5m" | "15m"
    yes_token_id: str
    no_token_id:  str
    end_date_iso: str
    best_yes_ask: float = 0.0
    best_no_ask:  float = 0.0


# ── Dynamic asset keyword generator ───────────────────────────────────────────

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

def _keywords_for(asset: str) -> List[str]:
    """Return search keywords for an asset ticker."""
    asset = asset.lower()
    return _ASSET_ALIASES.get(asset, [asset])


def _match_asset(question: str, assets: List[str]) -> Optional[str]:
    """Return the matching asset ticker from the configured list, or None."""
    q = question.lower()
    for asset in assets:
        for kw in _keywords_for(asset):
            if kw in q:
                logger.debug("Market '%s' matched asset '%s' via keyword '%s'", question[:60], asset, kw)
                return asset
    return None


def _match_interval(question: str, target: str) -> bool:
    return any(kw in question.lower() for kw in INTERVAL_KEYWORDS.get(target, []))


def _match_direction(question: str) -> Optional[str]:
    q = question.lower()
    if any(kw in q for kw in _UP_KEYWORDS):
        return "up"
    if any(kw in q for kw in _DOWN_KEYWORDS):
        return "down"
    return None


# ── CLOB pagination ────────────────────────────────────────────────────────────

def _fetch_page(next_cursor: str = "") -> dict:
    params: dict = {"active": "true", "closed": "false"}
    if next_cursor:
        params["next_cursor"] = next_cursor
    logger.debug("Fetching CLOB markets page cursor='%s'", next_cursor or "start")
    resp = requests.get(f"{CLOB_BASE}/markets", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Public API ─────────────────────────────────────────────────────────────────

def discover_markets(
    interval:  str = "5m",
    assets:    List[str] | None = None,
    max_pages: int = 20,
) -> List[PolyMarket]:
    """
    Page through Polymarket CLOB and return matching markets.
    Assets not found on Polymarket are logged as WARNING — never crash.
    """
    if assets is None:
        from .price_feed import _ASSET_ALIASES
        assets = list(_ASSET_ALIASES.keys())

    assets_lower = [a.lower() for a in assets]
    found_assets: set = set()
    markets: List[PolyMarket] = []
    cursor = ""
    pages  = 0

    logger.info("Starting market discovery — interval=%s assets=%s", interval, assets_lower)

    while pages < max_pages:
        try:
            data = _fetch_page(cursor)
        except Exception as exc:
            logger.error("CLOB fetch error on page %d: %s", pages, exc)
            break

        page_markets = data.get("data", [])
        logger.debug("Page %d: %d raw markets", pages, len(page_markets))

        for m in page_markets:
            question: str = m.get("question", "")
            asset = _match_asset(question, assets_lower)
            if asset is None:
                continue
            if not _match_interval(question, interval):
                continue
            direction = _match_direction(question)
            if direction is None:
                logger.debug("Skipping market (no direction): %s", question[:80])
                continue

            tokens = m.get("tokens", [])
            if len(tokens) < 2:
                logger.debug("Skipping market (missing tokens): %s", question[:80])
                continue

            yes_tok = tokens[0].get("token_id", "")
            no_tok  = tokens[1].get("token_id", "")

            markets.append(PolyMarket(
                condition_id=m.get("condition_id", ""),
                question=question,
                asset=asset,
                direction=direction,
                interval=interval,
                yes_token_id=yes_tok,
                no_token_id=no_tok,
                end_date_iso=m.get("end_date_iso", ""),
            ))
            found_assets.add(asset)
            logger.debug("Found market: [%s/%s] %s", asset, direction, question[:80])

        cursor = data.get("next_cursor", "")
        if not cursor or cursor == "LTE=":
            logger.debug("Market discovery complete after %d pages", pages + 1)
            break
        pages += 1
        time.sleep(0.25)

    # Warn about assets that had NO Polymarket market at all
    missing = set(assets_lower) - found_assets
    for asset in missing:
        logger.warning(
            "Asset '%s' is in config but has NO active %s Up/Down market on Polymarket — "
            "it will be skipped this cycle.", asset, interval
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
            ob   = clob_client.get_order_book(m.yes_token_id)
            asks = ob.get("asks", [])
            if asks:
                m.best_yes_ask = float(asks[0]["price"])
            ob2   = clob_client.get_order_book(m.no_token_id)
            asks2 = ob2.get("asks", [])
            if asks2:
                m.best_no_ask = float(asks2[0]["price"])
            logger.debug("Prices enriched for %s/%s: yes=%.3f no=%.3f",
                         m.asset, m.direction, m.best_yes_ask, m.best_no_ask)
        except Exception as exc:
            logger.warning("Price enrichment failed for %s: %s", m.condition_id, exc)
    return markets
