"""
market_discovery.py — Fetch all active 5-min and 15-min Up/Down markets from
the Polymarket CLOB API and return structured market objects.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

# Keywords that appear in 5-min / 15-min crypto Up/Down market titles
INTERVAL_KEYWORDS = {
    "5m": ["5-minute", "5 minute", "5min", "next 5"],
    "15m": ["15-minute", "15 minute", "15min", "next 15"],
}
ASSET_MAP = {
    "btc": ["bitcoin", "btc"],
    "eth": ["ethereum", "eth"],
    "sol": ["solana", "sol"],
    "xrp": ["xrp", "ripple"],
}


@dataclass
class PolyMarket:
    condition_id: str
    question: str
    asset: str          # btc | eth | sol | xrp
    direction: str      # up | down
    interval: str       # 5m | 15m
    yes_token_id: str
    no_token_id: str
    end_date_iso: str
    best_yes_ask: float = 0.0
    best_no_ask: float = 0.0


def _fetch_active_markets(next_cursor: str = "") -> dict:
    params: dict = {"active": "true", "closed": "false"}
    if next_cursor:
        params["next_cursor"] = next_cursor
    resp = requests.get(f"{CLOB_BASE}/markets", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _match_asset(question: str) -> Optional[str]:
    q = question.lower()
    for asset, keywords in ASSET_MAP.items():
        if any(kw in q for kw in keywords):
            return asset
    return None


def _match_interval(question: str, target: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in INTERVAL_KEYWORDS.get(target, []))


def _match_direction(question: str) -> Optional[str]:
    q = question.lower()
    if "up" in q or "higher" in q or "above" in q:
        return "up"
    if "down" in q or "lower" in q or "below" in q:
        return "down"
    return None


def discover_markets(
    interval: str = "5m",
    assets: List[str] | None = None,
    max_pages: int = 20,
) -> List[PolyMarket]:
    """
    Page through the CLOB API and return all matching Up/Down markets
    for the requested interval and assets.
    """
    assets = assets or list(ASSET_MAP.keys())
    markets: List[PolyMarket] = []
    cursor = ""
    pages = 0

    while pages < max_pages:
        try:
            data = _fetch_active_markets(cursor)
        except Exception as exc:
            logger.error("Error fetching markets page %d: %s", pages, exc)
            break

        for m in data.get("data", []):
            question: str = m.get("question", "")
            asset = _match_asset(question)
            if asset not in assets:
                continue
            if not _match_interval(question, interval):
                continue
            direction = _match_direction(question)
            if direction is None:
                continue

            tokens = m.get("tokens", [])
            if len(tokens) < 2:
                continue
            # tokens[0] = YES, tokens[1] = NO  (Polymarket convention)
            yes_tok = tokens[0].get("token_id", "")
            no_tok = tokens[1].get("token_id", "")

            markets.append(
                PolyMarket(
                    condition_id=m.get("condition_id", ""),
                    question=question,
                    asset=asset,
                    direction=direction,
                    interval=interval,
                    yes_token_id=yes_tok,
                    no_token_id=no_tok,
                    end_date_iso=m.get("end_date_iso", ""),
                )
            )

        cursor = data.get("next_cursor", "")
        if not cursor or cursor == "LTE=":
            break
        pages += 1
        time.sleep(0.25)  # polite rate limit

    logger.info(
        "Discovered %d %s markets for assets %s", len(markets), interval, assets
    )
    return markets


def enrich_with_prices(markets: List[PolyMarket], clob_client=None) -> List[PolyMarket]:
    """
    Optionally enrich market objects with live mid-prices from the CLOB.
    Pass clob_client=None to skip (used in backtesting / paper mode).
    """
    if clob_client is None:
        return markets
    for m in markets:
        try:
            ob = clob_client.get_order_book(m.yes_token_id)
            asks = ob.get("asks", [])
            if asks:
                m.best_yes_ask = float(asks[0]["price"])
            ob2 = clob_client.get_order_book(m.no_token_id)
            asks2 = ob2.get("asks", [])
            if asks2:
                m.best_no_ask = float(asks2[0]["price"])
        except Exception as exc:
            logger.debug("Price enrichment failed for %s: %s", m.condition_id, exc)
    return markets
