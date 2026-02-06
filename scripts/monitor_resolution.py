#!/usr/bin/env python3
"""
MoltMarkets Resolution Monitor

Monitors for markets that should have been resolved but are stuck.
Sends alerts via Telegram when markets are overdue.

Usage:
    python scripts/monitor_resolution.py              # Check once
    python scripts/monitor_resolution.py --watch      # Continuous monitoring
    python scripts/monitor_resolution.py --alert-test # Test alerting
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

import requests

# API Configuration
API_BASE = os.getenv("MOLTMARKETS_API_URL", "https://moltmarkets-api-production.up.railway.app")

# State file to track overdue markets
STATE_FILE = os.path.expanduser("~/.config/moltmarkets/monitor_state.json")

# Alert thresholds
OVERDUE_THRESHOLD_MINUTES = 10  # Alert if market stuck for this long
CRITICAL_THRESHOLD_MINUTES = 30  # Escalate if stuck this long

# Telegram alerting (optional)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_ALERT_CHAT_ID")


def load_state() -> Dict:
    """Load monitoring state from file."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"overdue_markets": {}, "last_check": None, "alerts_sent": {}}


def save_state(state: Dict):
    """Save monitoring state to file."""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_overdue_markets() -> List[Dict]:
    """Fetch markets that are past close time but still OPEN."""
    try:
        resp = requests.get(f"{API_BASE}/markets", params={"status": "open"}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        now = datetime.now(timezone.utc)
        overdue = []
        
        for market in data.get("data", []):
            closes_at = datetime.fromisoformat(market["closes_at"].replace("Z", "+00:00"))
            if closes_at < now:
                minutes_overdue = (now - closes_at).total_seconds() / 60
                market["minutes_overdue"] = round(minutes_overdue, 1)
                overdue.append(market)
        
        return overdue
    except Exception as e:
        print(f"Error fetching markets: {e}", file=sys.stderr)
        return []


def send_telegram_alert(message: str) -> bool:
    """Send alert via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT] {message}")
        return False
    
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"Telegram alert failed: {e}", file=sys.stderr)
        return False


def format_market_alert(market: Dict, level: str = "WARNING") -> str:
    """Format a market into an alert message."""
    emoji = "⚠️" if level == "WARNING" else "🚨"
    return (
        f"{emoji} <b>MoltMarkets Resolution {level}</b>\n\n"
        f"<b>Market:</b> {market['title'][:60]}...\n"
        f"<b>ID:</b> <code>{market['id']}</code>\n"
        f"<b>Overdue:</b> {market['minutes_overdue']:.0f} minutes\n"
        f"<b>Creator:</b> {market.get('creator_username', 'unknown')}\n"
    )


def check_and_alert(state: Dict) -> Dict:
    """Check for overdue markets and send alerts as needed."""
    overdue = get_overdue_markets()
    now = datetime.now(timezone.utc).isoformat()
    
    print(f"[{now[:19]}] Found {len(overdue)} overdue market(s)")
    
    for market in overdue:
        market_id = market["id"]
        minutes = market["minutes_overdue"]
        
        # Track when we first saw this market as overdue
        if market_id not in state["overdue_markets"]:
            state["overdue_markets"][market_id] = {
                "first_seen": now,
                "title": market["title"][:60],
                "minutes_overdue": minutes
            }
            print(f"  NEW: {market['title'][:50]}... ({minutes:.0f}m overdue)")
        else:
            state["overdue_markets"][market_id]["minutes_overdue"] = minutes
        
        # Check if we should alert
        alert_key = f"{market_id}:{minutes // OVERDUE_THRESHOLD_MINUTES}"
        
        if minutes >= CRITICAL_THRESHOLD_MINUTES and alert_key not in state["alerts_sent"]:
            msg = format_market_alert(market, "CRITICAL")
            send_telegram_alert(msg)
            state["alerts_sent"][alert_key] = now
            print(f"  🚨 CRITICAL ALERT sent for {market_id}")
        
        elif minutes >= OVERDUE_THRESHOLD_MINUTES and alert_key not in state["alerts_sent"]:
            msg = format_market_alert(market, "WARNING")
            send_telegram_alert(msg)
            state["alerts_sent"][alert_key] = now
            print(f"  ⚠️ WARNING ALERT sent for {market_id}")
    
    # Clean up resolved markets from state
    current_ids = {m["id"] for m in overdue}
    resolved = [mid for mid in state["overdue_markets"] if mid not in current_ids]
    for mid in resolved:
        print(f"  ✅ Resolved: {state['overdue_markets'][mid]['title']}")
        del state["overdue_markets"][mid]
    
    state["last_check"] = now
    return state


def main():
    parser = argparse.ArgumentParser(description="Monitor MoltMarkets resolution")
    parser.add_argument("--watch", action="store_true", help="Continuous monitoring (every 2 min)")
    parser.add_argument("--alert-test", action="store_true", help="Send test alert")
    parser.add_argument("--interval", type=int, default=120, help="Watch interval in seconds")
    args = parser.parse_args()
    
    if args.alert_test:
        test_msg = "🧪 <b>MoltMarkets Monitor Test</b>\n\nAlert system is working!"
        if send_telegram_alert(test_msg):
            print("Test alert sent successfully")
        else:
            print("Test alert sent to console (no Telegram configured)")
        return
    
    state = load_state()
    
    if args.watch:
        import time
        print(f"Starting continuous monitoring (interval: {args.interval}s)")
        print("Press Ctrl+C to stop\n")
        
        while True:
            try:
                state = check_and_alert(state)
                save_state(state)
                time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nStopping monitor")
                break
    else:
        state = check_and_alert(state)
        save_state(state)
        
        # Summary
        if state["overdue_markets"]:
            print(f"\n⚠️  {len(state['overdue_markets'])} market(s) currently overdue")
        else:
            print("\n✅ No overdue markets")


if __name__ == "__main__":
    main()
