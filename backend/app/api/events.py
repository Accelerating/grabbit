"""Authenticated Server-Sent Events endpoint.

The router is intentionally created by :func:`create_events_router` so the
application lifespan can own one EventBus and publish from its scheduler.  A
small default snapshot is provided for the standalone router; applications can
inject a richer snapshot provider when wiring it into ``main.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from app.api.deps import AuthSession, Db
from app.api.tasks import ACTIVE, task_json
from app.core.config import Settings, get_settings
from app.db.models import RuntimeControl, Session, Setting, Task, WorkItem
from app.services.event_bus import Event, EventBus, SubscriptionClosed


SnapshotFactory = Callable[..., dict[str, Any] | Awaitable[dict[str, Any]]]


async def default_snapshot(request: Request, auth: Any, db: Any, settings: Settings) -> dict[str, Any]:
    """Build the bounded snapshot required by the SSE contract."""

    rows = (await db.scalars(select(Task).where(Task.status.in_(ACTIVE)).order_by(Task.created_at, Task.id).limit(100))).all()
    queue_revision = await db.get(Setting, "queue_revision")
    queued_items = await db.scalar(select(func.count()).select_from(WorkItem))
    control = await db.get(RuntimeControl, 1)
    scheduler = getattr(request.app.state, "scheduler", None)
    try:
        disk_free = shutil.disk_usage(settings.download_root).free
    except OSError:
        disk_free = None
    speed_bps = sum(int((json.loads(task.progress_json or "{}").get("speed_bps") or 0)) for task in rows)
    blocked_reason = control.blocked_reason if control and control.dispatch_suspended else None
    if not blocked_reason and scheduler is not None and not scheduler.available:
        blocked_reason = "dependency_missing"
    return {
        "system": {
            "download_speed_bps": speed_bps,
            "upload_speed_bps": 0,
            "disk_free_bytes": disk_free,
            "memory_available_bytes": None,
            "running_tasks": len(scheduler.active) if scheduler else 0,
            "queued_items": int(queued_items or 0),
            "blocked_reason": blocked_reason,
            "server_time": datetime.now(timezone.utc).isoformat(),
        },
        "tasks": [task_json(task) for task in rows],
        "queue_revision": int(queue_revision.value_json) if queue_revision else 0,
    }


def _sse_snapshot(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: snapshot\ndata: {payload}\n\n"


async def _call_snapshot(
    factory: SnapshotFactory,
    request: Request,
    auth: Any,
    db: Any,
    settings: Settings,
) -> dict[str, Any]:
    """Call providers written with either the documented or short signature.

    The documented signature is ``(request, auth, db, settings)``.  Supporting
    zero/one-argument providers keeps this route straightforward to exercise in
    unit tests and lets an application use a precomputed snapshot if desired.
    """

    signature = inspect.signature(factory)
    values = {"request": request, "auth": auth, "session": auth, "db": db, "settings": settings}
    positional: list[Any] = []
    keyword: dict[str, Any] = {}
    has_varargs = False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            has_varargs = True
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            continue
        value = values.get(parameter.name)
        if value is None and parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.name not in values:
            raise TypeError(f"Unsupported snapshot provider parameter: {parameter.name}")
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            keyword[parameter.name] = value
        else:
            positional.append(value)
    if has_varargs:
        positional.extend((request, auth, db, settings))
    result = factory(*positional, **keyword)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise TypeError("snapshot provider must return a dictionary")
    return result


def create_events_router(event_bus: EventBus, snapshot_factory: SnapshotFactory | None = None) -> APIRouter:
    """Create the ``GET /events`` router for one application EventBus.

    The parent app should include the result below the ``/api/v1`` prefix and
    publish updates through the same ``event_bus`` instance::

        app.include_router(create_events_router(bus), prefix="/api/v1")

    ``snapshot_factory`` may be an async callable accepting
    ``(request, auth, db, settings)`` and returning ``system``, ``tasks`` and
    ``queue_revision`` fields.
    """

    router = APIRouter(tags=["events"])
    provider = snapshot_factory or default_snapshot

    @router.get("/events")
    async def events(
        request: Request,
        auth: AuthSession,
        db: Db,
        settings: Annotated[Settings, Depends(get_settings)],
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        cursor: Annotated[str | None, Query(max_length=100)] = None,
    ) -> StreamingResponse:
        subscription = await event_bus.subscribe(last_event_id=last_event_id or cursor, user_id=auth.user_id)

        async def stream():
            try:
                snapshot = await _call_snapshot(provider, request, auth, db, settings)
                yield _sse_snapshot(snapshot)
                # Snapshot is deliberately unnumbered.  Replay IDs therefore
                # remain monotonic in the order in which the client receives
                # them, even if snapshot creation takes a little while.
                for replay_item in subscription.replay:
                    yield replay_item.as_sse()
                while True:
                    try:
                        item: Event = await asyncio.wait_for(subscription.get(), event_bus.heartbeat_interval)
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        if auth.expires_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc):
                            return
                        try:
                            session_id = await db.scalar(
                                select(Session.id).where(
                                    Session.id == auth.id,
                                    Session.expires_at > datetime.now(timezone.utc),
                                )
                            )
                        except Exception:
                            # A failed session check must not leave a stream
                            # alive indefinitely with an unknown auth state.
                            return
                        if session_id is None:
                            return
                        continue
                    except SubscriptionClosed:
                        return
                    yield item.as_sse()
            finally:
                await subscription.close("client_disconnected")

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


event_bus = EventBus()
router = create_events_router(event_bus)

__all__ = ["SnapshotFactory", "create_events_router", "default_snapshot", "event_bus", "router"]
