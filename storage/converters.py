"""
MoltMarkets Storage — row-to-dict converters.

Converts raw database rows into application-level dictionaries.
"""

import json
from typing import Any, Dict, Optional

from models import MarketStatus, Outcome
from storage.types import BetDict, MarketDict, PositionDict, UserDict


class ConverterMixin:
    """Mixin providing _row_to_* converter methods."""

    def _row_to_user(self, row: Optional[Dict[str, Any]]) -> Optional[UserDict]:
        """Convert database row to user dict."""
        if not row:
            return None
        return UserDict(
            id=row["id"],
            username=row["username"],
            display_name=row["display_name"] or row["username"],
            description=row["description"] or "",
            balance=float(row["balance"]),
            created_at=row["created_at"],
            markets_created=row["markets_created"],
            total_bets=row["total_bets"],
            profit_all_time=float(row["profit_all_time"]),
            api_key_hash=row["api_key_hash"],
            status=row.get("status", "pending"),
            verification_code=row.get("verification_code"),
            last_market_created_at=row.get("last_market_created_at"),
            twitter_handle=row.get("twitter_handle"),
            user_type=row.get("user_type", "agent"),
            is_sandbox=bool(row.get("is_sandbox", False)),
        )

    def _row_to_market(self, row: Optional[Dict[str, Any]]) -> Optional[MarketDict]:
        """Convert database row to market dict."""
        if not row:
            return None
        # Parse committee JSON if present
        committee_raw = row.get("committee")
        committee: Optional[list[str]] = None
        if committee_raw:
            try:
                committee = json.loads(committee_raw) if isinstance(committee_raw, str) else committee_raw
            except (json.JSONDecodeError, TypeError):
                committee = None
        return MarketDict(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=MarketStatus(row["status"].upper()),
            closes_at=row["closes_at"],
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            resolution=Outcome(row["resolution"].upper()) if row["resolution"] else None,
            total_volume=float(row["total_volume"]),
            creator_id=row["creator_id"],
            pool={"YES": float(row["pool_yes"]), "NO": float(row["pool_no"])},
            p=float(row["p"]),
            version=int(row.get("version", 1)),
            committee=committee,
            resolution_deadline=row.get("resolution_deadline"),
            last_traded_at=row.get("last_traded_at"),
        )

    def _row_to_bet(self, row: Optional[Dict[str, Any]]) -> Optional[BetDict]:
        """Convert database row to bet dict."""
        if not row:
            return None
        return BetDict(
            id=row["id"],
            market_id=row["market_id"],
            user_id=row["user_id"],
            outcome=Outcome(row["outcome"].upper()),
            amount=float(row["amount"]),
            shares=float(row["shares"]),
            avg_price=float(row["avg_price"]) if row["avg_price"] else 0,
            probability_before=float(row["probability_before"]) if row["probability_before"] else 0,
            probability_after=float(row["probability_after"]) if row["probability_after"] else 0,
            created_at=row["created_at"],
        )

    def _row_to_position(self, row: Optional[Dict[str, Any]]) -> Optional[PositionDict]:
        """Convert database row to position dict."""
        if not row:
            return None
        return PositionDict(
            market_id=row["market_id"],
            user_id=row["user_id"],
            yes_shares=float(row["yes_shares"]),
            no_shares=float(row["no_shares"]),
            total_invested=float(row["total_invested"]),
        )
