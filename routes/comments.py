"""
Comment endpoints — get and create comments on markets.
"""

import logging
import uuid

from fastapi import APIRouter, Depends

from auth import require_auth
from deps import get_db, validate_uuid
from errors import error_response, ErrorCode
from event_bus import event_bus, SSEEvent
from models import CommentCreate, Comment, MarketComments

logger = logging.getLogger(__name__)

router = APIRouter(tags=["markets"])


@router.get("/markets/{market_id}/comments", response_model=MarketComments)
async def get_comments(market_id: str):
    """Get all comments for a market."""
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    raw_comments = db.get_market_comments(market_id)

    comments_by_id = {}
    top_level = []

    for c in raw_comments:
        comment = Comment(
            id=c["id"],
            market_id=c["market_id"],
            user_id=c["user_id"],
            username=c.get("username", "unknown"),
            content=c["content"],
            created_at=c["created_at"],
            parent_id=c.get("parent_id"),
            replies=[],
        )
        comments_by_id[c["id"]] = comment
        if c.get("parent_id") is None:
            top_level.append(comment)

    for c in raw_comments:
        if c.get("parent_id") and c["parent_id"] in comments_by_id:
            parent = comments_by_id[c["parent_id"]]
            parent.replies.append(comments_by_id[c["id"]])

    return MarketComments(
        market_id=market_id,
        comments=top_level,
        total=len(raw_comments),
    )


@router.post("/markets/{market_id}/comments", response_model=Comment)
async def create_comment(market_id: str, req: CommentCreate, user: dict = Depends(require_auth)):
    """Create a comment on a market."""
    db = get_db()
    validate_uuid(market_id, "market_id")
    market = db.get_market(market_id)
    if not market:
        return error_response(404, "Market not found", ErrorCode.MARKET_NOT_FOUND)

    if req.parent_id:
        parent_comments = db.get_market_comments(market_id)
        if not any(c["id"] == req.parent_id for c in parent_comments):
            return error_response(400, "Parent comment not found", ErrorCode.COMMENT_NOT_FOUND)

    comment_id = str(uuid.uuid4())
    comment = db.create_comment(
        comment_id=comment_id,
        market_id=market_id,
        user_id=user["id"],
        content=req.content,
        parent_id=req.parent_id,
    )

    await event_bus.publish(SSEEvent(
        event="chat_message",
        data={
            "id": comment["id"],
            "username": user["username"],
            "text": comment["content"],
            "channel": "comment",
            "market_id": market_id,
            "created_at": str(comment["created_at"]),
        },
        market_id=market_id,
    ))

    return Comment(
        id=comment["id"],
        market_id=comment["market_id"],
        user_id=comment["user_id"],
        username=user["username"],
        content=comment["content"],
        created_at=comment["created_at"],
        parent_id=comment.get("parent_id"),
        replies=[],
    )
