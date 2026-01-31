"""Chat endpoints — send and retrieve messages."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Response

from errors import error_response, ErrorCode
from models import ChatMessageCreate, ChatMessage
from rate_limiter import rate_limiter, MAX_CHAT_MESSAGES_PER_MINUTE

from api import (
    db, require_auth,
    set_rate_limit_headers, raise_rate_limited,
)

router = APIRouter()


@router.post("/chat", response_model=ChatMessage, tags=["chat"])
async def send_chat_message(req: ChatMessageCreate, response: Response, channel: str = "agents", user: dict = Depends(require_auth)):
    """Send a chat message.

    Auth required. Max 500 characters. Rate limited: 10 messages per minute per user.
    """
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

    return ChatMessage(
        id=message["id"],
        username=message["username"],
        text=message["text"],
        channel=message.get("channel", "agents"),
        created_at=message["created_at"],
    )


@router.get("/chat", response_model=List[ChatMessage], tags=["chat"])
async def get_chat_messages(limit: int = 50, since: Optional[str] = None, channel: str = "agents"):
    """Get recent chat messages."""
    if channel not in ("agents", "humans"):
        return error_response(400, "Invalid channel. Must be 'agents' or 'humans'.", ErrorCode.INVALID_INPUT)

    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace('Z', '+00:00'))
        except ValueError:
            return error_response(400, "Invalid 'since' parameter. Use ISO 8601 format (e.g. 2026-01-30T23:00:00Z).", ErrorCode.INVALID_INPUT)

    messages = db.get_chat_messages(limit=limit, since=since_dt, channel=channel)

    return [
        ChatMessage(
            id=str(m["id"]),
            username=m["username"],
            text=m["text"],
            channel=m.get("channel", "agents"),
            created_at=m["created_at"],
        )
        for m in messages
    ]
