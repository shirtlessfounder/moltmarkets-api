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

# Shutdown signal — set by lifespan to gracefully close SSE connections
shutdown_event: asyncio.Event | None = None


def set_shutdown_event(event: asyncio.Event):
    """Called from lifespan to register the shutdown signal."""
    global shutdown_event
    shutdown_event = event


async def _event_stream(request: Request, market_id: Optional[str] = None):
    """Async generator that yields SSE-formatted text.

    Subscribes to the global event bus, yields events as they arrive,
    and sends ``:keepalive`` comments every 30 s to prevent proxies
    (Railway, Cloudflare, nginx) from closing the connection.
    """
    sub = event_bus.subscribe(market_id=market_id)
    try:
        # Send an initial comment so the client knows the connection is live
        yield ": connected\n\n"

        while True:
            # Check if server is shutting down
            if shutdown_event and shutdown_event.is_set():
                logger.info("sse_shutdown_signal", market_id=market_id)
                break

            # Check if the client disconnected
            if await request.is_disconnected():
                break

            try:
                # Wait for an event with a timeout for keepalive
                event = await asyncio.wait_for(
                    event_bus.listen(sub).__anext__(),
                    timeout=KEEPALIVE_INTERVAL,
                )
                yield event.to_sse()
            except asyncio.TimeoutError:
                # No event within the keepalive window — send a comment ping
                yield ": keepalive\n\n"
            except StopAsyncIteration:
                break
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
