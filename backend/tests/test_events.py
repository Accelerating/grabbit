from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.events import create_events_router
from app.db.models import Session
from app.services.event_bus import EventBus, SubscriptionClosed


@pytest.mark.asyncio
async def test_event_ids_replay_and_bounded_resync():
    bus = EventBus(history_size=2, queue_size=4)
    first = await bus.publish("task.updated", {"task_id": "one", "revision": 1})
    second = await bus.publish("queue.updated", {"revision": 2})
    await bus.publish("system.updated", {"download_speed_bps": 3})

    assert first.id.startswith(f"{bus.boot_uuid}:")
    assert second.sequence == first.sequence + 1
    replay = await bus.subscribe(last_event_id=second.id, user_id="user")
    assert [item.event for item in replay.replay] == ["system.updated"]
    await replay.close()

    expired = await bus.subscribe(last_event_id="%s:0" % bus.boot_uuid, user_id="user")
    assert len(expired.replay) == 1
    assert expired.replay[0].event == "resync_required"
    assert expired.replay[0].data == {"reason": "cursor_expired"}
    await expired.close()

    changed_boot = await bus.subscribe(last_event_id="other:1", user_id="user")
    assert changed_boot.replay[0].data["reason"] == "boot_changed"
    await changed_boot.close()


@pytest.mark.asyncio
async def test_slow_subscriber_is_closed_at_queue_limit():
    bus = EventBus(history_size=10, queue_size=2)
    subscription = await bus.subscribe(user_id="user")
    await bus.publish("task.updated", {"revision": 1})
    await bus.publish("task.updated", {"revision": 2})
    await bus.publish("task.updated", {"revision": 3})
    assert subscription.close_reason == "slow_consumer"
    assert bus.subscriber_count == 0
    with pytest.raises(SubscriptionClosed):
        await subscription.get()


@pytest.mark.asyncio
async def test_disconnect_user_wakes_stream():
    bus = EventBus()
    subscription = await bus.subscribe(user_id="user")
    waiter = asyncio.create_task(subscription.get())
    await bus.disconnect_user("user")
    with pytest.raises(SubscriptionClosed):
        await waiter


@pytest.mark.asyncio
async def test_events_route_writes_snapshot_then_replay_and_heartbeat():
    bus = EventBus(heartbeat_interval=0.01)
    cursor = await bus.publish("system.updated", {"download_speed_bps": 0})
    old = await bus.publish("queue.updated", {"revision": 1})
    auth = Session(id="session", user_id="user", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))

    async def provider():
        return {"system": {}, "tasks": [], "queue_revision": 1}

    app = FastAPI()
    app.include_router(create_events_router(bus, provider))
    route = create_events_router(bus, provider).routes[0]
    request = __import__("starlette.requests", fromlist=["Request"]).Request(
        {"type": "http", "method": "GET", "path": "/events", "headers": [], "client": ("127.0.0.1", 1), "scheme": "http", "server": ("test", 80)}
    )
    class FakeDb:
        async def scalar(self, _query):
            return auth.id

    response = await route.endpoint(request, auth, FakeDb(), object(), cursor.id)
    iterator = response.body_iterator.__aiter__()
    assert "event: snapshot" in await iterator.__anext__()
    replay = await iterator.__anext__()
    assert "event: queue.updated" in replay
    assert "id: " in replay
    heartbeat = await iterator.__anext__()
    assert heartbeat == ": heartbeat\n\n"
    await iterator.aclose()

    query_response = await route.endpoint(request, auth, FakeDb(), object(), None, cursor.id)
    query_iterator = query_response.body_iterator.__aiter__()
    assert "event: snapshot" in await query_iterator.__anext__()
    assert "event: queue.updated" in await query_iterator.__anext__()
    await query_iterator.aclose()
