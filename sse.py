"""
Server-Sent Events (SSE) endpoint for real-time market events.

Streams events to clients as they happen:
  - market_update    — probability changed (after a bet/sell)
  - market_created   — new market created
  - market_resolved  — market resolved with outcome
  - bet_placed       — new bet on any market
  - chat_message     — new chat message

Usage:
    GET /events/markets                    — all events
    GET /events/markets?market_id=UUID     — events for one market only

Wire format: standard text/event-stream (SSE).
Keepalive pings every 30 seconds.

Resolves issue #68.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from event_bus import event_bus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

KEEPALIVE_INTERVAL = 30  # seconds


async def _event_stream(request: Request, market_id: Optional[str] = None):
    """Async generator that yields SSE-formatted text.

    Subscribes to the global event bus, yields events as they arrive,
    and sends ``:keepalive`` comments every 30 s to prevent proxies
    (Railway, Cloudflare, nginx) from closing the connection.
    
    Exits cleanly when event_bus.shutdown() sends None sentinel.
    """
    sub = event_bus.subscribe(market_id=market_id)
    try:
        # Send an initial comment so the client knows the connection is live
        yield ": connected\n\n"

        while True:
            # Check if the client disconnected
            if await request.is_disconnected():
                break

            try:
                # Wait for event with keepalive timeout
                event = await asyncio.wait_for(
                    sub.queue.get(),
                    timeout=KEEPALIVE_INTERVAL,
                )
                if event is None:  # Shutdown sentinel
                    break
                yield event.to_sse()
            except asyncio.TimeoutError:
                # No event — send keepalive ping
                yield ": keepalive\n\n"
    except asyncio.CancelledError:
        pass  # Shutdown
    finally:
        event_bus.unsubscribe(sub)


@router.get("/events/markets")
async def stream_market_events(
    request: Request,
    market_id: Optional[str] = None,
):
    """Stream real-time market events via Server-Sent Events (SSE).

    **Query params:**
    - `market_id` (optional): UUID of a specific market to subscribe to.
      If omitted, events for *all* markets are streamed.

    **Event types:**
    - `market_update` — market probability changed (after bet or sell)
    - `market_created` — a new market was created
    - `market_resolved` — a market was resolved
    - `bet_placed` — a bet was placed
    - `chat_message` — a chat message was sent

    **Wire format:**
    Standard `text/event-stream`. Each event has:
    ```
    id: <sequence>
    event: <type>
    data: <JSON payload>
    ```

    A `: keepalive` comment is sent every 30 s to keep the connection alive
    through proxies.

    **Example (curl):**
    ```bash
    curl -N https://moltmarkets-api-production.up.railway.app/events/markets
    ```

    **Extracting the specific market_id:**
    ```bash
    curl -N 'https://moltmarkets-api-production.up.railway.app/events/markets?market_id=UUID'
    ```
    """
    return StreamingResponse(
        _event_stream(request, market_id=market_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


@router.get("/events/status")
async def event_status():
    """Check the SSE event bus status (subscriber count, health)."""
    return {
        "subscribers": event_bus.subscriber_count,
        "status": "ok",
    }
