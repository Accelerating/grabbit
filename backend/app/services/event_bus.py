"""In-process event fan-out for the authenticated progress stream.

The event bus deliberately has bounded memory.  It is intended for UI updates,
not as a durable job log; REST endpoints remain the source of truth after a
restart or a long disconnect.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Event:
    """One SSE event kept in the short replay buffer."""

    id: str
    event: str
    data: dict[str, Any]
    recipient_user_id: str | None = None

    @property
    def sequence(self) -> int:
        return int(self.id.rsplit(":", 1)[1])

    def as_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.event}\ndata: {payload}\n\n"


class Subscription:
    """A subscriber's bounded live queue.

    ``replay`` is kept separate so a route can write its initial snapshot
    first, then write replayed events, while events published during snapshot
    generation remain in the live queue.
    """

    def __init__(
        self,
        bus: EventBus,
        user_id: str | None,
        replay: tuple[Event, ...],
        queue_size: int,
    ) -> None:
        self.bus = bus
        self.user_id = user_id
        self.replay = replay
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=queue_size)
        self.closed = asyncio.Event()
        self.close_reason: str | None = None

    @property
    def queue_size(self) -> int:
        return self.queue.maxsize

    async def get(self) -> Event:
        """Wait for the next event, raising ``SubscriptionClosed`` on close."""

        if self.closed.is_set():
            raise SubscriptionClosed(self.close_reason)
        get_task = asyncio.create_task(self.queue.get())
        close_task = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait({get_task, close_task}, return_when=asyncio.FIRST_COMPLETED)
            if close_task in done:
                get_task.cancel()
                await asyncio.gather(get_task, return_exceptions=True)
                raise SubscriptionClosed(self.close_reason)
            item = get_task.result()
            if item is None:
                raise SubscriptionClosed(self.close_reason)
            return item
        finally:
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)

    async def close(self, reason: str = "closed") -> None:
        await self.bus._close_subscription(self, reason)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        try:
            return await self.get()
        except SubscriptionClosed as exc:
            raise StopAsyncIteration from exc


class SubscriptionClosed(Exception):
    """Raised when a client is revoked or its queue overflows."""


class EventBus:
    """Bounded replay buffer and bounded per-client fan-out queues.

    Event IDs use one boot UUID and a monotonically increasing sequence.  A
    subscriber can replay only events still in this process's bounded buffer;
    callers must resync through REST when :attr:`Subscription.resync_reason` is
    set.
    """

    def __init__(self, *, history_size: int = 1000, queue_size: int = 100, heartbeat_interval: float = 15.0) -> None:
        if history_size < 1:
            raise ValueError("history_size must be positive")
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        self.boot_uuid = str(uuid4())
        self.history_size = history_size
        self.queue_size = queue_size
        self.heartbeat_interval = heartbeat_interval
        self._sequence = 0
        self._history: deque[Event] = deque(maxlen=history_size)
        self._subscribers: set[Subscription] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def current_sequence(self) -> int:
        return self._sequence

    @property
    def history(self) -> tuple[Event, ...]:
        """A read-only copy, useful for diagnostics and deterministic tests."""

        return tuple(self._history)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _next_event(self, event: str, data: dict[str, Any], recipient_user_id: str | None = None) -> Event:
        self._sequence += 1
        return Event(f"{self.boot_uuid}:{self._sequence}", event, data, recipient_user_id)

    async def publish(self, event: str, data: dict[str, Any], *, user_id: str | None = None) -> Event:
        """Publish a small JSON event and return its assigned event ID.

        ``user_id`` scopes delivery to one authenticated account when given;
        the event is still retained in the replay buffer for that account.
        """

        if not isinstance(data, dict):
            raise TypeError("event data must be a dictionary")
        async with self._lock:
            if self._closed:
                raise RuntimeError("event bus is closed")
            item = self._next_event(event, data, user_id)
            self._history.append(item)
            overflowed: list[Subscription] = []
            for subscriber in tuple(self._subscribers):
                if subscriber.user_id is not None and user_id is not None and subscriber.user_id != user_id:
                    continue
                if subscriber.user_id is not None and user_id is None:
                    # A public event is visible to all authenticated users.
                    pass
                try:
                    subscriber.queue.put_nowait(item)
                except asyncio.QueueFull:
                    overflowed.append(subscriber)
            for subscriber in overflowed:
                self._remove_subscription(subscriber, "slow_consumer")
            return item

    async def subscribe(self, *, last_event_id: str | None = None, user_id: str | None = None) -> Subscription:
        """Register a subscriber and calculate a bounded replay.

        A replay gap, an unknown boot UUID, or an invalid cursor produces one
        private ``resync_required`` event in ``subscription.replay``.  It is
        intentionally not added to global history.
        """

        async with self._lock:
            if self._closed:
                raise RuntimeError("event bus is closed")
            replay, reason = self._replay_for(last_event_id, user_id)
            if reason:
                replay = (self._next_event("resync_required", {"reason": reason}),)
            subscription = Subscription(self, user_id, replay, self.queue_size)
            self._subscribers.add(subscription)
            return subscription

    def _replay_for(self, last_event_id: str | None, user_id: str | None) -> tuple[tuple[Event, ...], str | None]:
        if not last_event_id:
            return (), None
        try:
            boot, raw_sequence = last_event_id.rsplit(":", 1)
            sequence = int(raw_sequence)
        except (AttributeError, ValueError):
            return (), "invalid_cursor"
        if boot != self.boot_uuid or sequence < 0:
            return (), "boot_changed"
        if sequence > self._sequence:
            return (), "cursor_ahead"
        if not self._history:
            return (), None if sequence == self._sequence else "cursor_expired"
        first = self._history[0].sequence
        if sequence < first - 1:
            return (), "cursor_expired"
        return tuple(
            item for item in self._history
            if item.sequence > sequence and (item.recipient_user_id is None or item.recipient_user_id == user_id)
        ), None

    async def _close_subscription(self, subscription: Subscription, reason: str) -> None:
        async with self._lock:
            self._remove_subscription(subscription, reason)

    def _remove_subscription(self, subscription: Subscription, reason: str) -> None:
        self._subscribers.discard(subscription)
        subscription.close_reason = reason
        subscription.closed.set()
        # Wake a consumer that is currently waiting.  If the queue is full it
        # will observe the closed event instead.
        try:
            subscription.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def disconnect_user(self, user_id: str, *, reason: str = "session_revoked") -> None:
        """Close all streams belonging to a revoked/expired account."""

        async with self._lock:
            for subscriber in tuple(self._subscribers):
                if subscriber.user_id == user_id:
                    self._remove_subscription(subscriber, reason)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for subscriber in tuple(self._subscribers):
                self._remove_subscription(subscriber, "server_shutdown")


__all__ = ["Event", "EventBus", "Subscription", "SubscriptionClosed"]
