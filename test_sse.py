"""
Tests for the SSE event bus and endpoint.

Run: pytest test_sse.py -v
"""

import asyncio
import pytest

from event_bus import EventBus, SSEEvent


@pytest.fixture
def bus():
    return EventBus()


# ---------------------------------------------------------------------------
# EventBus unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_delivers_to_subscriber(bus):
    sub = bus.subscribe()
    event = SSEEvent(event="test", data={"key": "value"})
    delivered = await bus.publish(event)
    assert delivered == 1
    received = await asyncio.wait_for(bus.listen(sub).__anext__(), timeout=1)
    assert received.event == "test"
    assert received.data == {"key": "value"}
    bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_market_id_filter(bus):
    sub_all = bus.subscribe()
    sub_abc = bus.subscribe(market_id="abc")

    event_abc = SSEEvent(event="bet_placed", data={"x": 1}, market_id="abc")
    event_xyz = SSEEvent(event="bet_placed", data={"x": 2}, market_id="xyz")

    await bus.publish(event_abc)
    await bus.publish(event_xyz)

    # sub_all should receive both
    e1 = await asyncio.wait_for(bus.listen(sub_all).__anext__(), timeout=1)
    e2 = await asyncio.wait_for(bus.listen(sub_all).__anext__(), timeout=1)
    assert {e1.data["x"], e2.data["x"]} == {1, 2}

    # sub_abc should only receive event_abc
    e = await asyncio.wait_for(bus.listen(sub_abc).__anext__(), timeout=1)
    assert e.data["x"] == 1

    # sub_abc should have nothing else
    assert sub_abc.queue.empty()

    bus.unsubscribe(sub_all)
    bus.unsubscribe(sub_abc)


@pytest.mark.asyncio
async def test_unsubscribe_removes_subscriber(bus):
    sub = bus.subscribe()
    assert bus.subscriber_count == 1
    bus.unsubscribe(sub)
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_global_event_reaches_filtered_subscriber(bus):
    """Events with market_id=None should reach all subscribers."""
    sub = bus.subscribe(market_id="abc")
    global_event = SSEEvent(event="chat_message", data={"msg": "hi"}, market_id=None)
    await bus.publish(global_event)
    e = await asyncio.wait_for(bus.listen(sub).__anext__(), timeout=1)
    assert e.data["msg"] == "hi"
    bus.unsubscribe(sub)


@pytest.mark.asyncio
async def test_multiple_subscribers(bus):
    subs = [bus.subscribe() for _ in range(5)]
    event = SSEEvent(event="test", data={"n": 1})
    delivered = await bus.publish(event)
    assert delivered == 5
    for sub in subs:
        e = await asyncio.wait_for(bus.listen(sub).__anext__(), timeout=1)
        assert e.data["n"] == 1
        bus.unsubscribe(sub)


def test_sse_serialization():
    event = SSEEvent(event="market_update", data={"probability": 0.65}, id="42")
    text = event.to_sse()
    assert "id: 42" in text
    assert "event: market_update" in text
    assert '"probability": 0.65' in text
    # Must end with double newline
    assert text.endswith("\n\n")


# ---------------------------------------------------------------------------
# FastAPI endpoint integration test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sse_event_stream_generator():
    """Test the raw _event_stream generator produces correct SSE output."""
    from unittest.mock import AsyncMock
    from sse import _event_stream
    from event_bus import event_bus as global_bus

    # Create a mock request that is never disconnected
    mock_request = AsyncMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)

    # Schedule an event after a short delay
    async def publish_then_stop():
        await asyncio.sleep(0.1)
        await global_bus.publish(SSEEvent(
            event="bet_placed",
            data={"market_id": "test-123", "amount": 50},
        ))

    task = asyncio.create_task(publish_then_stop())

    chunks = []
    async for chunk in _event_stream(mock_request):
        chunks.append(chunk)
        if len(chunks) >= 2:  # ": connected\n\n" + the event
            break

    await task
    full = "".join(chunks)
    assert "connected" in full
    assert "bet_placed" in full
    assert "test-123" in full


@pytest.mark.asyncio
async def test_event_status_endpoint():
    from httpx import AsyncClient, ASGITransport
    from api import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/events/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "subscribers" in data
        assert data["status"] == "ok"
