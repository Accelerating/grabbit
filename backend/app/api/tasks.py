from __future__ import annotations

import hashlib
import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Annotated
from urllib.parse import urlsplit, urlunsplit, unquote, parse_qsl
from uuid import uuid4

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from app.api.deps import AuthSession, CsrfSession, Db
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.paths import safe_subdirectory
from app.db.models import Aria2Binding, CookieProfile, FileRecord, IdempotencyKey, RuntimeControl, Setting, Task, TaskAttempt, TaskGroup, TaskSource, WorkItem, utcnow
from app.db.session import SessionLocal

router = APIRouter(tags=["tasks"])
ACTIVE = {"queued", "resolving", "awaiting_selection", "downloading", "postprocessing", "retry_wait", "paused"}
TERMINAL = {"completed", "failed", "cancelled", "stopped"}


async def publish_changes(request: Request, tasks: list[Task] | None = None, queue_revision: int | None = None) -> None:
    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:
        return
    for task in tasks or []:
        await bus.publish("task.updated", {"task_id": task.id, "revision": task.revision, "status": task.status, "phase": task.phase, "progress": json.loads(task.progress_json or "{}"), "pending_action": task.pending_action})
    if queue_revision is not None:
        await bus.publish("queue.updated", {"revision": queue_revision})


class GeneralCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[str] = Field(min_length=1, max_length=100)
    download_subdir: str = Field(default="general", min_length=1, max_length=255)
    allow_duplicates: bool = False


class DuplicateCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[str] = Field(min_length=1, max_length=100)
    mode: str = "general"


def normalize_http(raw: str) -> tuple[str, str, str]:
    if len(raw) > 8192 or any(ord(char) < 32 for char in raw):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid URL")
    parts = urlsplit(raw.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ApiError(422, "VALIDATION_ERROR", "Only HTTP and HTTPS URLs are supported here")
    if parts.username or parts.password:
        raise ApiError(422, "VALIDATION_ERROR", "Embedded URL credentials are not supported")
    try:
        port = parts.port
    except ValueError as exc:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid URL port") from exc
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    normalized = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
    title = unquote(PurePosixPath(parts.path).name) or parts.hostname
    title = title[:255]
    return normalized, fingerprint, title


def normalize_magnet(raw: str) -> tuple[str, str, str]:
    if len(raw) > 8192 or any(ord(char) < 32 for char in raw):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid magnet URI")
    parts = urlsplit(raw.strip())
    if parts.scheme.lower() != "magnet" or parts.netloc or not parts.query:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid magnet URI")
    query = parse_qsl(parts.query, keep_blank_values=True)
    hashes = [value[9:] for key, value in query if key == "xt" and value.lower().startswith("urn:btih:")]
    if len(hashes) != 1:
        raise ApiError(422, "VALIDATION_ERROR", "Magnet URI needs one BTIH hash")
    raw_hash = hashes[0]
    if len(raw_hash) == 40:
        try:
            infohash = bytes.fromhex(raw_hash).hex()
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid BTIH hash") from exc
    elif len(raw_hash) == 32:
        try:
            infohash = base64.b32decode(raw_hash.upper(), casefold=True).hex()
        except (ValueError, binascii.Error) as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid BTIH hash") from exc
    else:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid BTIH hash")
    title = next((value[:255] for key, value in query if key == "dn" and value), infohash)
    if any(ord(char) < 32 for char in title):
        title = infohash
    return raw.strip(), infohash, title


def normalize_source(raw: str) -> tuple[str, str, str, str]:
    if raw.strip().lower().startswith("magnet:"):
        url, fingerprint, title = normalize_magnet(raw)
        return url, fingerprint, title, "magnet"
    url, fingerprint, title = normalize_http(raw)
    return url, fingerprint, title, "http"


def allowed_actions(task: Task) -> list[str]:
    if task.pending_action:
        return []
    if task.kind == "video":
        return {
            "queued": ["stop"], "retry_wait": ["stop"], "downloading": ["stop"],
            "stopped": ["retry"], "failed": ["retry"], "completed": [],
        }.get(task.status, [])
    return {
        "queued": ["pause", "cancel"], "resolving": ["pause", "cancel"],
        "downloading": ["pause", "cancel"], "retry_wait": ["pause", "cancel"],
        "paused": ["resume", "cancel"], "failed": ["retry", "cancel"],
        "awaiting_selection": ["cancel"],
        "cancelled": ["retry"], "completed": ["redownload"],
    }.get(task.status, [])


def task_json(task: Task) -> dict:
    site = None
    if task.kind == "video" and task.source_url:
        from app.services.video import video_site
        try:
            site = video_site(task.source_url)
        except ApiError:
            pass
    return {
        "id": task.id, "kind": task.kind, "source_type": task.source_type, "title": task.title, "group_id": task.group_id,
        "status": task.status, "phase": task.phase, "pending_action": task.pending_action,
        "cookie_profile_id": task.cookie_profile_id, "cookie_name_snapshot": task.cookie_name_snapshot,
        "source_site": site,
        "blocked_reason": task.blocked_reason, "progress": json.loads(task.progress_json or "{}"),
        "warnings": json.loads(task.warnings_json or "[]"), "error_code": task.error_code,
        "error_summary": task.error_summary, "allowed_actions": allowed_actions(task),
        "revision": task.revision, "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


async def bump_queue_revision(db) -> int:
    row = await db.get(Setting, "queue_revision")
    if row is None:
        db.add(Setting(key="queue_revision", value_json="1", revision=1))
        return 1
    value = int(row.value_json) + 1
    row.value_json = str(value)
    row.revision += 1
    return value


@router.post("/sources/check-duplicates")
async def check_duplicates(body: DuplicateCheck, db: Db, auth: AuthSession) -> dict:
    if body.mode != "general":
        raise ApiError(422, "VALIDATION_ERROR", "Unsupported mode")
    items = []
    for source in body.sources:
        _, fingerprint, _, _ = normalize_source(source)
        matches = (await db.scalars(select(Task).where(Task.source_fingerprint == fingerprint).order_by(Task.created_at.desc()).limit(5))).all()
        items.append({"source": source, "matches": [task_json(task) for task in matches]})
    return {"items": items}


@router.post("/tasks/general", status_code=201)
async def create_general(
    body: GeneralCreate, auth: CsrfSession, request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    settings = get_settings()
    safe_subdirectory(settings.download_root, body.download_subdir)
    normalized = [normalize_source(raw) for raw in body.sources]
    if len(set(item[1] for item in normalized)) != len(normalized) and not body.allow_duplicates:
        raise ApiError(409, "DUPLICATE_SOURCE", "Batch contains duplicate sources")
    payload_hash = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    if idempotency_key and len(idempotency_key) > 128:
        raise ApiError(422, "VALIDATION_ERROR", "Idempotency key is too long")
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        if idempotency_key:
            prior = await db.get(IdempotencyKey, (auth.user_id, idempotency_key))
            if prior is not None and prior.expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
                if prior.request_hash != payload_hash:
                    raise ApiError(409, "STATE_CONFLICT", "Idempotency key was used for a different request")
                return json.loads(prior.response_json)
        fingerprints = [item[1] for item in normalized]
        existing = (await db.scalars(select(Task).where(Task.source_fingerprint.in_(fingerprints)).limit(101))).all()
        if existing and not body.allow_duplicates:
            raise ApiError(409, "DUPLICATE_SOURCE", "One or more sources already exist", {"matching_task_ids": [task.id for task in existing]})
        max_position = await db.scalar(select(func.max(WorkItem.position))) or 0
        now = utcnow()
        tasks = []
        for offset, (url, fingerprint, title, source_type) in enumerate(normalized, 1):
            task_id = str(uuid4())
            directory_key = f"{body.download_subdir}/{task_id}"
            task = Task(
                id=task_id, kind="general", source_type=source_type, source_url=url,
                source_fingerprint=fingerprint, title=title, status="queued",
                download_subdir=body.download_subdir, task_directory_key=directory_key,
                progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]",
                created_at=now, updated_at=now,
            )
            db.add(task)
            if source_type == "magnet":
                db.add(TaskSource(task_id=task_id, magnet_infohash=fingerprint, source_snapshot_json="{}"))
            db.add(WorkItem(work_type="metadata" if source_type == "magnet" else "download", resource_id=task_id, position=max_position + offset, available_at=now))
            tasks.append(task)
        await db.flush()
        queue_revision = await bump_queue_revision(db)
        response = {"items": [task_json(task) for task in tasks]}
        if idempotency_key:
            db.add(IdempotencyKey(
                user_id=auth.user_id, key=idempotency_key, request_hash=payload_hash,
                response_json=json.dumps(response), expires_at=now + timedelta(hours=24),
            ))
        await db.commit()
        await publish_changes(request, tasks, queue_revision)
        return response


@router.get("/tasks")
async def list_tasks(
    db: Db, auth: AuthSession, kind: str | None = None, status: str | None = None,
    query: str | None = None, limit: int = Query(default=30, ge=1, le=100), cursor: str | None = None,
) -> dict:
    statement = select(Task).order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)
    if kind:
        statement = statement.where(Task.kind == kind)
    if status:
        statement = statement.where(Task.status == status)
    else:
        statement = statement.where(Task.status.in_(ACTIVE | {"failed"}))
    if query:
        statement = statement.where(Task.title.contains(query[:100]))
    if cursor:
        try:
            created_text, task_id = cursor.split("|", 1)
            created = datetime.fromisoformat(created_text)
        except (ValueError, TypeError) as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid cursor") from exc
        statement = statement.where((Task.created_at < created) | ((Task.created_at == created) & (Task.id < task_id)))
    rows = (await db.scalars(statement)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = f"{rows[-1].created_at.isoformat()}|{rows[-1].id}" if has_more and rows else None
    return {"items": [task_json(row) for row in rows], "next_cursor": next_cursor}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, db: Db, auth: AuthSession) -> dict:
    task = await db.get(Task, task_id)
    if task is None:
        raise ApiError(404, "NOT_FOUND", "Task not found")
    return task_json(task)


class CookieChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookie_profile_id: str | None = None
    revision: int = Field(ge=1)


@router.patch("/tasks/{task_id}/cookie")
async def change_task_cookie(task_id: str, body: CookieChange, request: Request, auth: CsrfSession) -> dict:
    from app.services.video import video_site

    scheduler = getattr(request.app.state, "scheduler", None)
    lock = scheduler._lock if scheduler else None
    if lock:
        await lock.acquire()
    try:
        async with SessionLocal() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            task = await db.get(Task, task_id)
            if task is None:
                raise ApiError(404, "NOT_FOUND", "Task not found")
            if task.kind != "video" or task.status not in {"queued", "retry_wait", "stopped", "failed"} or task.pending_action:
                raise ApiError(409, "STATE_CONFLICT", "Cookie can only be changed on a non-running video task")
            if task.revision != body.revision:
                raise ApiError(409, "REVISION_CONFLICT", "Task changed; refresh and try again")
            profile = await db.get(CookieProfile, body.cookie_profile_id) if body.cookie_profile_id else None
            if body.cookie_profile_id and (profile is None or profile.site_key != video_site(task.source_url or "")):
                raise ApiError(422, "COOKIE_SITE_MISMATCH", "Cookie profile does not belong to this site")
            task.cookie_profile_id = profile.id if profile else None
            task.cookie_name_snapshot = profile.name if profile else None
            task.revision += 1
            task.updated_at = utcnow()
            await db.commit()
            await publish_changes(request, [task])
            return task_json(task)
    finally:
        if lock:
            lock.release()


@router.get("/tasks/{task_id}/files")
async def task_files(task_id: str, db: Db, auth: AuthSession, limit: int = Query(default=30, ge=1, le=100), cursor: int = Query(default=0, ge=0)) -> dict:
    task = await db.get(Task, task_id)
    if task is None:
        raise ApiError(404, "NOT_FOUND", "Task not found")
    if task.source_type == "magnet" and task.status == "awaiting_selection":
        source = await db.get(TaskSource, task.id)
        snapshot = json.loads(source.source_snapshot_json or "{}") if source else {}
        files = snapshot.get("files", [])
        page = files[cursor:cursor + limit]
        return {"items": page, "next_cursor": str(cursor + limit) if cursor + limit < len(files) else None, "total": len(files), "metadata_revision": task.revision}
    files = (await db.scalars(select(FileRecord).where(FileRecord.task_id == task_id).order_by(FileRecord.relative_path).limit(limit))).all()
    return {"items": [{
        "id": file.id, "name": file.display_name, "relative_path": file.relative_path,
        "size_bytes": file.size_bytes, "availability": file.availability,
        "is_complete": file.is_complete,
    } for file in files], "next_cursor": None}


class SelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_indices: list[int] = Field(min_length=1, max_length=20_000)
    metadata_revision: int = Field(ge=1)


@router.put("/tasks/{task_id}/selection")
async def select_magnet_files(task_id: str, body: SelectionRequest, request: Request, auth: CsrfSession) -> dict:
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task = await db.get(Task, task_id)
        if task is None:
            raise ApiError(404, "NOT_FOUND", "Task not found")
        if task.source_type != "magnet" or task.status != "awaiting_selection":
            raise ApiError(409, "STATE_CONFLICT", "Task is not awaiting magnet file selection")
        if task.revision != body.metadata_revision:
            raise ApiError(409, "REVISION_CONFLICT", "Metadata changed; refresh file list")
        source = await db.get(TaskSource, task.id)
        snapshot = json.loads(source.source_snapshot_json or "{}") if source else {}
        valid = {item["index"] for item in snapshot.get("files", [])}
        indices = body.selected_indices
        if len(set(indices)) != len(indices) or not set(indices) <= valid:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid selected file indices")
        task.selection_json = json.dumps({"selected_indices": sorted(indices)})
        task.status = "queued"
        task.revision += 1
        task.updated_at = utcnow()
        position = (await db.scalar(select(func.max(WorkItem.position))) or 0) + 1
        db.add(WorkItem(work_type="download", resource_id=task.id, position=position))
        revision = await bump_queue_revision(db)
        await db.commit()
        await publish_changes(request, [task], revision)
        return task_json(task)


@router.get("/queue")
async def get_queue(db: Db, auth: AuthSession, limit: int = Query(default=30, ge=1, le=100)) -> dict:
    rows = (await db.execute(select(WorkItem, Task).join(Task, WorkItem.resource_id == Task.id).order_by(WorkItem.position).limit(limit))).all()
    revision_row = await db.get(Setting, "queue_revision")
    control = await db.get(RuntimeControl, 1)
    return {"items": [{"id": work.id, "position": work.position, "task": task_json(task)} for work, task in rows], "revision": int(revision_row.value_json) if revision_row else 0, "blocked_reason": control.blocked_reason if control and control.dispatch_suspended else None}


class QueueMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direction: str
    revision: int = Field(ge=0)


@router.post("/queue/{work_id}/move")
async def move_queue_item(work_id: str, body: QueueMove, auth: CsrfSession, request: Request) -> dict:
    if body.direction not in {"up", "down", "top"}:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid queue movement")
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        row = await db.get(Setting, "queue_revision")
        current_revision = int(row.value_json) if row else 0
        if body.revision != current_revision:
            raise ApiError(409, "REVISION_CONFLICT", "Queue changed; refresh and try again")
        items = (await db.scalars(select(WorkItem).order_by(WorkItem.position))).all()
        index = next((i for i, item in enumerate(items) if item.id == work_id), None)
        if index is None:
            raise ApiError(404, "NOT_FOUND", "Queue item not found")
        target = max(0, index - 1) if body.direction == "up" else min(len(items) - 1, index + 1) if body.direction == "down" else 0
        item = items.pop(index)
        items.insert(target, item)
        for position, queued in enumerate(items, 1):
            queued.position = -position
        await db.flush()
        for position, queued in enumerate(items, 1):
            queued.position = position
        queue_revision = await bump_queue_revision(db)
        await db.commit()
        await publish_changes(request, queue_revision=queue_revision)
        return {"revision": queue_revision}


@router.get("/history")
async def history(db: Db, auth: AuthSession, kind: str | None = None, limit: int = Query(default=30, ge=1, le=100), cursor: str | None = None) -> dict:
    statement = select(Task).where(Task.status.in_(TERMINAL)).order_by(Task.finished_at.desc(), Task.id.desc()).limit(limit + 1)
    if kind:
        statement = statement.where(Task.kind == kind)
    if cursor:
        try:
            finished_text, task_id = cursor.split("|", 1)
            finished = datetime.fromisoformat(finished_text)
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid cursor") from exc
        statement = statement.where((Task.finished_at < finished) | ((Task.finished_at == finished) & (Task.id < task_id)))
    rows = (await db.scalars(statement)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [task_json(row) for row in rows], "next_cursor": f"{rows[-1].finished_at.isoformat()}|{rows[-1].id}" if has_more and rows else None}


@router.post("/tasks/{task_id}/deletion-preview")
async def deletion_preview(task_id: str, db: Db, auth: CsrfSession) -> dict:
    task = await db.get(Task, task_id)
    if task is None:
        raise ApiError(404, "NOT_FOUND", "Task not found")
    if task.status not in TERMINAL:
        raise ApiError(409, "STATE_CONFLICT", "Stop or cancel the task before deleting its record")
    files = (await db.scalars(select(FileRecord).where(FileRecord.task_id == task_id).order_by(FileRecord.relative_path))).all()
    return {"task_id": task_id, "task_revision": task.revision, "files": [{"id": file.id, "name": file.display_name, "size_bytes": file.size_bytes} for file in files], "total_bytes": sum(file.size_bytes or 0 for file in files), "delete_files_supported": False}


@router.post("/tasks/{task_id}/retry-subtitles", status_code=202)
async def retry_subtitles(task_id: str, request: Request, auth: CsrfSession) -> dict:
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task = await db.get(Task, task_id)
        if task is None:
            raise ApiError(404, "NOT_FOUND", "Task not found")
        if task.kind != "video" or task.status != "completed" or task.pending_action:
            raise ApiError(409, "STATE_CONFLICT", "Only completed video tasks can retry subtitles")
        options = json.loads(task.options_json or "{}")
        if options.get("mode", "video") != "video" or not options.get("subtitle_languages", ["zh.*", "en.*"]):
            raise ApiError(409, "STATE_CONFLICT", "This task has no subtitle selection")
        existing = await db.scalar(select(WorkItem).where(WorkItem.work_type == "subtitle_retry", WorkItem.resource_id == task_id))
        if existing is not None:
            return {"id": existing.id, "task_id": task_id, "status": "running" if task.phase == "subtitle_retry" else "queued"}
        position = (await db.scalar(select(func.max(WorkItem.position))) or 0) + 1
        item = WorkItem(work_type="subtitle_retry", resource_id=task_id, position=position)
        db.add(item)
        task.phase = "subtitle_queued"
        task.revision += 1
        task.updated_at = utcnow()
        revision = await bump_queue_revision(db)
        await db.commit()
        await publish_changes(request, [task], revision)
        return {"id": item.id, "task_id": task_id, "status": "queued"}


@router.post("/tasks/{task_id}/{action}", status_code=202)
async def task_action(task_id: str, action: str, request: Request, auth: CsrfSession) -> dict:
    if action not in {"pause", "resume", "cancel", "retry", "stop"}:
        raise ApiError(404, "NOT_FOUND", "Task action not found")
    scheduler = getattr(request.app.state, "scheduler", None)
    lock = scheduler._lock if scheduler else None
    if lock:
        await lock.acquire()
    try:
        async with SessionLocal() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            task = await db.get(Task, task_id)
            if task is None:
                raise ApiError(404, "NOT_FOUND", "Task not found")
            if task.kind not in {"general", "video"}:
                raise ApiError(409, "STATE_CONFLICT", "Action is not available for this task type")
            if task.kind == "video" and action not in {"stop", "retry"}:
                raise ApiError(409, "STATE_CONFLICT", "Action is not available for this task type")
            if action not in allowed_actions(task):
                settled = {"pause": "paused", "resume": "queued", "cancel": "cancelled", "retry": "queued", "stop": "stopped"}
                if task.pending_action == action or task.status == settled[action]:
                    return task_json(task)
                raise ApiError(409, "STATE_CONFLICT", "Action is not available in the current state")
            queued = await db.scalar(select(WorkItem).where(WorkItem.resource_id == task.id))
            if action == "stop":
                if task.status == "downloading":
                    task.pending_action = "stop"
                else:
                    task.status = "stopped"
                    task.finished_at = utcnow()
                    task.next_retry_at = None
                    if queued:
                        await db.delete(queued)
            elif action == "pause":
                if task.status in {"downloading", "resolving"}:
                    task.pending_action = "pause"
                else:
                    task.status = "paused"
                    task.next_retry_at = None
                    if queued:
                        await db.delete(queued)
            elif action == "cancel":
                if task.status in {"downloading", "resolving"}:
                    task.pending_action = "cancel"
                else:
                    task.status = "cancelled"
                    task.finished_at = utcnow()
                    task.next_retry_at = None
                    if queued:
                        await db.delete(queued)
            elif action in {"resume", "retry"}:
                task.status = "queued"
                task.pending_action = None
                task.finished_at = None
                task.error_code = None
                task.error_summary = None
                task.next_retry_at = None
                if action == "retry":
                    task.retry_cycle += 1
                    task.attempt_count = 0
                if queued is None:
                    position = (await db.scalar(select(func.max(WorkItem.position))) or 0) + 1
                    work_type = "video" if task.kind == "video" else "metadata" if task.source_type == "magnet" and not json.loads(task.selection_json or "{}").get("selected_indices") else "download"
                    db.add(WorkItem(work_type=work_type, resource_id=task.id, position=position))
            task.revision += 1
            task.updated_at = utcnow()
            queue_revision = await bump_queue_revision(db) if queued is not None or action in {"resume", "retry"} else None
            await db.commit()
            await publish_changes(request, [task], queue_revision)
            return task_json(task)
    finally:
        if lock:
            lock.release()


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task_record(task_id: str, revision: int, auth: CsrfSession) -> None:
    """Delete a terminal history record only; retain every on-disk file."""
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        task = await db.get(Task, task_id)
        if task is None:
            raise ApiError(404, "NOT_FOUND", "Task not found")
        if task.status not in TERMINAL or task.pending_action:
            raise ApiError(409, "STATE_CONFLICT", "Task is not terminal")
        if task.revision != revision:
            raise ApiError(409, "REVISION_CONFLICT", "Task changed; preview again")
        if await db.scalar(select(WorkItem.id).where(WorkItem.resource_id == task_id).limit(1)) is not None:
            raise ApiError(409, "STATE_CONFLICT", "Task has queued work")
        for file in (await db.scalars(select(FileRecord).where(FileRecord.task_id == task_id))).all():
            file.task_id = None
        for binding in (await db.scalars(select(Aria2Binding).where(Aria2Binding.task_id == task_id))).all():
            await db.delete(binding)
        for attempt in (await db.scalars(select(TaskAttempt).where(TaskAttempt.task_id == task_id))).all():
            await db.delete(attempt)
        source = await db.get(TaskSource, task_id)
        if source:
            await db.delete(source)
        for work in (await db.scalars(select(WorkItem).where(WorkItem.resource_id == task_id))).all():
            await db.delete(work)
        group_id = task.group_id
        await db.delete(task)
        await db.flush()
        if group_id and await db.scalar(select(Task.id).where(Task.group_id == group_id).limit(1)) is None:
            group = await db.get(TaskGroup, group_id)
            if group is not None:
                await db.delete(group)
        await db.commit()
