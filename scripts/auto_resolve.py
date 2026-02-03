#!/usr/bin/env python3
"""
MoltMarkets Auto-Resolution System

Automatically resolves verifiable markets that are past their close time.

Supports:
- Crypto price markets (BTC, ETH, SOL) via CoinGecko API
- Can be extended for other oracle types (HN, sports, etc.)

Usage:
    python scripts/auto_resolve.py              # Dry run (default)
    python scripts/auto_resolve.py --execute    # Actually resolve markets
    python scripts/auto_resolve.py --market-id <id>  # Resolve specific market
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Optional, Tuple

import requests

# API Configuration
API_BASE = os.getenv("MOLTMARKETS_API_URL", "https://moltmarkets-api-production.up.railway.app")
ADMIN_SECRET = os.getenv("MOLTMARKETS_ADMIN_SECRET")
API_KEY = os.getenv("MOLTMARKETS_API_KEY")

# Load from credentials file if env vars not set
CREDS_FILE = os.path.expanduser("~/.config/moltmarkets/credentials.json")
if os.path.exists(CREDS_FILE) and (not ADMIN_SECRET or not API_KEY):
    with open(CREDS_FILE) as f:
        creds = json.load(f)
        API_KEY = API_KEY or creds.get("api_key")
        ADMIN_SECRET = ADMIN_SECRET or creds.get("admin_secret")
        API_BASE = creds.get("base_url", API_BASE)


# =============================================================================
# Crypto Price Oracle
# =============================================================================

CRYPTO_PATTERNS = {
    "BTC": [
        (r"BTC\s+(?:be\s+)?(?:above|over)\s*\$?([\d,]+)", "above"),
        (r"BTC\s+(?:be\s+)?(?:below|under)\s*\$?([\d,]+)", "below"),
        (r"Bitcoin\s+(?:be\s+)?(?:above|over)\s*\$?([\d,]+)", "above"),
        (r"Bitcoin\s+(?:be\s+)?(?:below|under)\s*\$?([\d,]+)", "below"),
    ],
    "ETH": [
        (r"ETH\s+(?:hold\s+)?(?:above|over)\s*\$?([\d,]+)", "above"),
        (r"ETH\s+(?:drop\s+)?(?:below|under)\s*\$?([\d,]+)", "below"),
        (r"Ethereum\s+(?:be\s+)?(?:above|over)\s*\$?([\d,]+)", "above"),
        (r"Ethereum\s+(?:be\s+)?(?:below|under)\s*\$?([\d,]+)", "below"),
    ],
    "SOL": [
        (r"SOL\s+(?:be\s+)?(?:above|over)\s*\$?([\d,]+)", "above"),
        (r"SOL\s+(?:drop\s+)?(?:below|under)\s*\$?([\d,]+)", "below"),
        (r"Solana\s+(?:be\s+)?(?:above|over)\s*\$?([\d,]+)", "above"),
        (r"Solana\s+(?:be\s+)?(?:below|under)\s*\$?([\d,]+)", "below"),
    ],
}

COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
}


def parse_crypto_market(title: str, description: str) -> Optional[dict]:
    """
    Parse a market title/description to extract crypto resolution criteria.
    
    Returns:
        {
            "asset": "BTC" | "ETH" | "SOL",
            "threshold": 79000.0,
            "direction": "above" | "below",
            "yes_condition": "price_above" | "price_below"
        }
    """
    for asset, pattern_list in CRYPTO_PATTERNS.items():
        for pattern, direction in pattern_list:
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                threshold = float(match.group(1).replace(",", ""))
                
                # Determine yes_condition based on direction
                if direction == "above":
                    yes_condition = "price_above"
                else:
                    yes_condition = "price_below"
                
                return {
                    "asset": asset,
                    "threshold": threshold,
                    "direction": direction,
                    "yes_condition": yes_condition,
                }
    
    return None


# Global price cache to avoid repeated API calls
_price_cache: dict = {}
_price_cache_time: Optional[datetime] = None

# Binance trading pairs
BINANCE_PAIRS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}


def get_all_crypto_prices() -> dict:
    """Fetch all crypto prices. Tries CoinGecko first, falls back to Binance."""
    global _price_cache, _price_cache_time
    
    # Use cache if less than 60 seconds old
    now = datetime.now(timezone.utc)
    if _price_cache_time and (now - _price_cache_time).total_seconds() < 60:
        return _price_cache
    
    # Try CoinGecko first
    coin_ids = ",".join(COINGECKO_IDS.values())
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_ids, "vs_currencies": "usd"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            result = {}
            for asset, coin_id in COINGECKO_IDS.items():
                if coin_id in data:
                    result[asset] = data[coin_id].get("usd")
            
            if result:
                _price_cache = result
                _price_cache_time = now
                return result
    except Exception as e:
        print(f"  ⚠️ CoinGecko failed: {e}")
    
    # Fallback to Binance API (reliable, high rate limits)
    print("  📡 Falling back to Binance API...")
    result = {}
    try:
        # Binance ticker endpoint - get all prices in one call
        resp = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Build a symbol -> price map
            price_map = {item["symbol"]: float(item["price"]) for item in data}
            
            for asset, pair in BINANCE_PAIRS.items():
                if pair in price_map:
                    result[asset] = price_map[pair]
    except Exception as e:
        print(f"  ⚠️ Binance failed: {e}")
    
    if result:
        _price_cache = result
        _price_cache_time = now
    
    return result if result else _price_cache


def get_crypto_price(asset: str) -> Optional[float]:
    """Fetch current price (uses batch cache with fallback to CoinCap)."""
    prices = get_all_crypto_prices()
    return prices.get(asset)


def resolve_crypto_market(market: dict) -> Tuple[Optional[str], str]:
    """
    Determine resolution for a crypto price market.
    
    Returns:
        (outcome, reasoning) where outcome is "YES", "NO", or None if unresolvable
    """
    parsed = parse_crypto_market(market["title"], market.get("description", ""))
    if not parsed:
        return None, "Could not parse crypto resolution criteria from title"
    
    current_price = get_crypto_price(parsed["asset"])
    if current_price is None:
        return None, f"Failed to fetch {parsed['asset']} price"
    
    threshold = parsed["threshold"]
    
    if parsed["yes_condition"] == "price_above":
        # YES if price is above threshold
        if current_price > threshold:
            outcome = "YES"
            reasoning = f"{parsed['asset']} price ${current_price:,.2f} > ${threshold:,.0f} threshold"
        else:
            outcome = "NO"
            reasoning = f"{parsed['asset']} price ${current_price:,.2f} ≤ ${threshold:,.0f} threshold"
    else:  # price_below
        # YES if price dropped below threshold
        if current_price < threshold:
            outcome = "YES"
            reasoning = f"{parsed['asset']} price ${current_price:,.2f} < ${threshold:,.0f} threshold"
        else:
            outcome = "NO"
            reasoning = f"{parsed['asset']} price ${current_price:,.2f} ≥ ${threshold:,.0f} threshold"
    
    return outcome, reasoning


# =============================================================================
# HN Oracle (for Hacker News markets)
# =============================================================================

def parse_hn_market(title: str, description: str) -> Optional[dict]:
    """Parse HN story point threshold markets."""
    # Pattern: "Will the HN #1 story 'X' hit Y points within Z hour?"
    match = re.search(r"HN.*?(?:hit|reach)\s+(\d+)\s+points", title, re.IGNORECASE)
    if match:
        threshold = int(match.group(1))
        # Try to extract story title for lookup
        story_match = re.search(r"'([^']+)'", title)
        story_title = story_match.group(1) if story_match else None
        return {
            "type": "hn_points",
            "threshold": threshold,
            "story_title": story_title,
        }
    return None


def get_hn_story_points(story_title: str) -> Optional[Tuple[int, str]]:
    """Search HN for a story by title and return its current points.
    
    Returns:
        (points, story_url) or None if not found
    """
    try:
        # Search Algolia HN API for the story
        resp = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": story_title,
                "tags": "story",
                "hitsPerPage": 5,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        
        # Find the best match
        for hit in data.get("hits", []):
            if story_title.lower() in hit.get("title", "").lower():
                return hit.get("points", 0), f"https://news.ycombinator.com/item?id={hit['objectID']}"
        
        # If no exact match, return the first result if title is similar
        if data.get("hits"):
            hit = data["hits"][0]
            return hit.get("points", 0), f"https://news.ycombinator.com/item?id={hit['objectID']}"
        
        return None
    except Exception as e:
        print(f"  ⚠️ HN API error: {e}")
        return None


def resolve_hn_market(market: dict) -> Tuple[Optional[str], str]:
    """
    Attempt to resolve HN market by checking story points via Algolia API.
    """
    parsed = parse_hn_market(market["title"], market.get("description", ""))
    if not parsed:
        return None, "Could not parse HN resolution criteria"
    
    story_title = parsed.get("story_title")
    if not story_title:
        return None, "Could not extract story title from market"
    
    result = get_hn_story_points(story_title)
    if result is None:
        return None, f"Could not find HN story '{story_title}'"
    
    points, url = result
    threshold = parsed["threshold"]
    
    if points >= threshold:
        return "YES", f"HN story has {points} points (≥ {threshold} threshold). Story: {url}"
    else:
        return "NO", f"HN story has {points} points (< {threshold} threshold). Story: {url}"


# =============================================================================
# Market Classification & Resolution
# =============================================================================

def classify_market(market: dict) -> str:
    """Classify market type based on title/description."""
    title = market["title"].lower()
    
    # Crypto price markets
    for asset in ["btc", "bitcoin", "eth", "ethereum", "sol", "solana"]:
        if asset in title:
            return "crypto"
    
    # HN markets
    if "hn" in title or "hacker news" in title:
        return "hn"
    
    return "unknown"


def get_resolution(market: dict) -> Tuple[Optional[str], str, str]:
    """
    Get the resolution for a market.
    
    Returns:
        (outcome, reasoning, resolution_type)
        outcome: "YES", "NO", or None if unresolvable
        resolution_type: "auto" or "manual"
    """
    market_type = classify_market(market)
    
    if market_type == "crypto":
        outcome, reasoning = resolve_crypto_market(market)
        return outcome, reasoning, "auto" if outcome else "manual"
    
    if market_type == "hn":
        outcome, reasoning = resolve_hn_market(market)
        return outcome, reasoning, "auto" if outcome else "manual"
    
    return None, "Unknown market type - requires manual resolution", "manual"


# =============================================================================
# API Functions
# =============================================================================

def get_expired_open_markets() -> list:
    """Fetch all OPEN markets that are past their closes_at time."""
    resp = requests.get(f"{API_BASE}/markets", params={"limit": 100}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    
    now = datetime.now(timezone.utc)
    expired = []
    
    for market in data.get("data", []):
        if market["status"] != "OPEN":
            continue
        
        closes_at = datetime.fromisoformat(market["closes_at"].replace("Z", "+00:00"))
        if closes_at < now:
            hours_past = (now - closes_at).total_seconds() / 3600
            market["_hours_past_close"] = hours_past
            expired.append(market)
    
    return expired


def resolve_market_api(market_id: str, outcome: str, market_status: str = "OPEN") -> dict:
    """Call the resolve endpoint with admin override.
    
    Handles the committee resolution flow:
    1. OPEN market → call resolve (may trigger committee window)
    2. RESOLVING with active window → use committee vote
    3. RESOLVING past deadline → admin can force resolve
    """
    if not ADMIN_SECRET or not API_KEY:
        raise ValueError("ADMIN_SECRET and API_KEY are required")
    
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "X-Admin-Secret": ADMIN_SECRET,
        "Content-Type": "application/json",
    }
    
    # First try the resolve endpoint
    resp = requests.post(
        f"{API_BASE}/markets/{market_id}/resolve",
        json={"outcome": outcome},
        headers=headers,
        timeout=30,
    )
    
    # Check for committee window active error
    if resp.status_code == 403:
        error_data = resp.json()
        if error_data.get("code") == "COMMITTEE_WINDOW_ACTIVE":
            # Try committee vote endpoint instead
            print("   📋 Committee window active, casting vote...")
            vote_resp = requests.post(
                f"{API_BASE}/markets/{market_id}/resolution-vote",
                json={"outcome": outcome},
                headers=headers,
                timeout=30,
            )
            
            if vote_resp.status_code == 200:
                vote_data = vote_resp.json()
                if vote_data.get("auto_resolved"):
                    print("   ✅ Committee vote triggered auto-resolution!")
                    return {"status": "RESOLVED", "resolution": outcome}
                else:
                    # Vote cast but no auto-resolve yet
                    return {
                        "status": "RESOLVING",
                        "message": f"Vote cast for {outcome}. Waiting for other committee members or deadline.",
                        "resolution": None,
                    }
            else:
                # If voting fails, raise the error
                vote_resp.raise_for_status()
    
    resp.raise_for_status()
    return resp.json()


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Auto-resolve expired MoltMarkets")
    parser.add_argument("--execute", action="store_true", help="Actually resolve (default is dry run)")
    parser.add_argument("--market-id", help="Resolve a specific market by ID")
    args = parser.parse_args()
    
    dry_run = not args.execute
    
    print("=" * 60)
    print("MoltMarkets Auto-Resolution System")
    print(f"Mode: {'DRY RUN' if dry_run else '🔴 EXECUTING RESOLUTIONS'}")
    print(f"API: {API_BASE}")
    print("=" * 60)
    
    if args.market_id:
        # Resolve specific market
        resp = requests.get(f"{API_BASE}/markets/{args.market_id}", timeout=30)
        if resp.status_code == 404:
            print(f"❌ Market {args.market_id} not found")
            return 1
        resp.raise_for_status()
        markets = [resp.json()]
    else:
        # Get all expired open markets
        markets = get_expired_open_markets()
    
    if not markets:
        print("\n✅ No expired OPEN markets found.")
        return 0
    
    print(f"\n📊 Found {len(markets)} expired market(s) to process:\n")
    
    auto_resolved = []
    manual_needed = []
    errors = []
    
    for market in markets:
        print("─" * 60)
        print(f"📌 {market['title']}")
        print(f"   ID: {market['id']}")
        print(f"   Closed: {market['closes_at']} ({market.get('_hours_past_close', 0):.1f}h ago)")
        print(f"   Type: {classify_market(market)}")
        
        outcome, reasoning, resolution_type = get_resolution(market)
        
        if resolution_type == "auto" and outcome:
            print(f"   ✅ Auto-resolution: {outcome}")
            print(f"   📝 Reason: {reasoning}")
            
            if not dry_run:
                try:
                    result = resolve_market_api(market["id"], outcome)
                    print(f"   🎯 Resolved! New status: {result.get('status')}")
                    auto_resolved.append({
                        "id": market["id"],
                        "title": market["title"],
                        "outcome": outcome,
                        "reasoning": reasoning,
                    })
                except Exception as e:
                    print(f"   ❌ Failed to resolve: {e}")
                    errors.append({"id": market["id"], "title": market["title"], "error": str(e)})
            else:
                auto_resolved.append({
                    "id": market["id"],
                    "title": market["title"],
                    "outcome": outcome,
                    "reasoning": reasoning,
                })
        else:
            print("   ⚠️ Needs manual resolution")
            print(f"   📝 Reason: {reasoning}")
            manual_needed.append({
                "id": market["id"],
                "title": market["title"],
                "reason": reasoning,
            })
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"{'Would auto-resolve' if dry_run else 'Auto-resolved'}: {len(auto_resolved)}")
    print(f"Needs manual resolution: {len(manual_needed)}")
    if errors:
        print(f"Errors: {len(errors)}")
    
    if manual_needed:
        print("\n⚠️ Markets needing manual resolution:")
        for m in manual_needed:
            print(f"  - {m['title'][:50]}... ({m['id'][:8]}...)")
            print(f"    Reason: {m['reason']}")
    
    if dry_run and auto_resolved:
        print(f"\n💡 Run with --execute to actually resolve {len(auto_resolved)} market(s)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
