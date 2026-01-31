"""
In-process event bus for Server-Sent Events (SSE).

Uses the broadcast pattern: one asyncio.Queue per connected client.
Publishing an event copies it into every subscriber's queue so each
SSE connection receives every event independently.

Thread-safe for FastAPI's async request handling.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class SSEEvent:
    """A single server-sent event."""
    event: str                          # Event type: market_update, bet_placed, etc.
    data: Dict[str, Any]                # JSON-serializable payload
    market_id: Optional[str] = None     # If set, only subscribers filtering on this market receive it
    id: Optional[str] = None            # SSE event id (for Last-Event-ID reconnection)
    timestamp: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        """Serialize to the SSE wire format (text/event-stream)."""
        lines = []
        if self.id:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event}")
        lines.append(f"data: {json.dumps(self.data, default=str)}")
        lines.append("")  # Blank line terminates the event
        return "\n".join(lines) + "\n"


class _Subscriber:
    """Represents one connected SSE client."""
    __slots__ = ("queue", "market_id", "created_at")

    def __init__(self, market_id: Optional[str] = None):
        self.queue: asyncio.Queue[SSEEvent] = asyncio.Queue(maxsize=256)
        self.market_id = market_id          # None = subscribe to all events
        self.created_at = time.time()


class EventBus:
    """Broadcast event bus.

    Usage::

        bus = EventBus()

        # Publisher (called from mutation endpoints):
        await bus.publish(SSEEvent(event="bet_placed", data={...}, market_id="abc"))

        # Subscriber (SSE endpoint):
        sub = bus.subscribe(market_id="abc")   # or None for all
        try:
            async for event in bus.listen(sub):
                yield event.to_sse()
        finally:
            bus.unsubscribe(sub)
    """

    def __init__(self):
        self._subscribers: Set[_Subscriber] = set()
        self._lock = asyncio.Lock()
        self._event_counter = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: SSEEvent) -> int:
        """Publish an event to all matching subscribers.

        Returns the number of subscribers that received the event.
        """
        self._event_counter += 1
        if event.id is None:
            event.id = str(self._event_counter)

        delivered = 0
        dead: list[_Subscriber] = []

        async with self._lock:
            for sub in self._subscribers:
                # Apply market_id filter: if subscriber filters on a specific
                # market, only deliver events for that market (or global events
                # with market_id=None).
                if sub.market_id and event.market_id and sub.market_id != event.market_id:
                    continue
                try:
                    sub.queue.put_nowait(event)
                    delivered += 1
                except asyncio.QueueFull:
                    # Slow consumer — drop them to avoid memory buildup
                    logger.warning("SSE subscriber queue full, dropping connection")
                    dead.append(sub)

            for sub in dead:
                self._subscribers.discard(sub)

        return delivered

    def subscribe(self, market_id: Optional[str] = None) -> _Subscriber:
        """Register a new subscriber. Returns a _Subscriber handle."""
        sub = _Subscriber(market_id=market_id)
        # We add synchronously; the set is only mutated under the async lock
        # during publish, but subscribe/unsubscribe are called from the same
        # event loop so there's no data race.
        self._subscribers.add(sub)
        logger.info(
            "SSE subscriber connected (market_id=%s, total=%d)",
            market_id or "all",
            len(self._subscribers),
        )
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        """Remove a subscriber."""
        self._subscribers.discard(sub)
        logger.info(
            "SSE subscriber disconnected (total=%d)",
            len(self._subscribers),
        )

    async def listen(self, sub: _Subscriber):
        """Async generator that yields events for a subscriber.

        Blocks on the subscriber's queue. The caller should wrap this in a
        try/finally to call unsubscribe() on disconnect.
        """
        while True:
            event = await sub.queue.get()
            yield event


# ---------------------------------------------------------------------------
# Global singleton — imported by api.py and sse.py
# ---------------------------------------------------------------------------
event_bus = EventBus()
