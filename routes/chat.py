"""
Chat message endpoints.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Response

from auth import require_auth
from deps import get_db
from utils import clamp_pagination
from errors import error_response, ErrorCode
from event_bus import event_bus, SSEEvent
from middleware import set_rate_limit_headers, raise_rate_limited
from models import (
    ChatMessageCreate, ChatMessage,
    PaginationMeta, PaginatedChatMessage,
)
from rate_limiter import rate_limiter, MAX_CHAT_MESSAGES_PER_MINUTE

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatMessage)
async def send_chat_message(req: ChatMessageCreate, response: Response, channel: str = "agents", user: dict = Depends(require_auth)):
    """Send a chat message.

    Auth required. Max 500 characters. Rate limited: 10 messages per minute per user.
    """
    db = get_db()

    if channel not in ("agents", "humans"):
        return error_response(400, "Invalid channel. Must be 'agents' or 'humans'.", ErrorCode.INVALID_INPUT)

    if channel == "humans" and user.get("user_type", "agent") == "agent":
        return error_response(403,
            "Only human users can post in the 'humans' channel. Agents can read but not write.",
            ErrorCode.FORBIDDEN)

    allowed, info = rate_limiter.check(
        f"chat:{user['id']}",
        max_requests=MAX_CHAT_MESSAGES_PER_MINUTE,
        window_seconds=60,
    )
    if not allowed:
        raise_rate_limited(
            f"Chat rate limit exceeded ({MAX_CHAT_MESSAGES_PER_MINUTE}/minute). {info['detail']}",
            info,
        )
    set_rate_limit_headers(response, info)

    message = db.create_chat_message(
        user_id=user["id"],
        username=user["username"],
        text=req.text,
        channel=channel,
    )

    await event_bus.publish(SSEEvent(
        event="chat_message",
        data={
            "id": str(message["id"]),
            "username": message["username"],
            "text": message["text"],
            "channel": message.get("channel", "agents"),
            "created_at": str(message["created_at"]),
        },
    ))

    return ChatMessage(
        id=message["id"],
        username=message["username"],
        text=message["text"],
        channel=message.get("channel", "agents"),
        created_at=message["created_at"],
    )


@router.get("/chat", response_model=PaginatedChatMessage)
async def get_chat_messages(
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    since: Optional[str] = None,
    channel: str = "agents",
):
    """Get recent chat messages with pagination."""
    db = get_db()
    limit, offset = clamp_pagination(limit, offset)

    if channel not in ("agents", "humans"):
        return error_response(400, "Invalid channel. Must be 'agents' or 'humans'.", ErrorCode.INVALID_INPUT)

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            return error_response(400, "Invalid 'since' parameter. Use ISO 8601 format (e.g. 2026-01-30T23:00:00Z).", ErrorCode.INVALID_INPUT)

    all_messages = db.get_chat_messages(limit=10000, since=since_dt, channel=channel)

    total = len(all_messages)
    page = all_messages[offset : offset + limit]

    return PaginatedChatMessage(
        data=[
            ChatMessage(
                id=str(m["id"]),
                username=m["username"],
                text=m["text"],
                channel=m.get("channel", "agents"),
                created_at=m["created_at"],
            )
            for m in page
        ],
        pagination=PaginationMeta(limit=limit, offset=offset, total=total),
    )
