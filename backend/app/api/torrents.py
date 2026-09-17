"""Torrent upload inspection endpoints.

Creating a download task from the selected file indices is deliberately a
later operation.  These endpoints only accept, validate and retain a private
metainfo blob plus its parsed file candidates.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Query, UploadFile
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func, text

from app.api.deps import AuthSession, CsrfSession, Db
from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.models import TorrentUpload, Task, TaskSource, WorkItem, utcnow
from app.api.tasks import bump_queue_revision, publish_changes, task_json
from app.core.paths import safe_subdirectory
from app.db.session import SessionLocal
from app.services.torrent import (
    MAX_UPLOAD_BYTES,
    TorrentParseError,
    parse_torrent,
    persist_torrent_blob,
    summary_json,
)

router = APIRouter(tags=["torrents"])
DEFAULT_FILE_LIMIT = 50


class TorrentTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    torrent_id: str
    selected_indices: list[int] = Field(min_length=1, max_length=20_000)
    download_subdir: str = Field(default="general", min_length=1, max_length=255)
    allow_duplicates: bool = False


@router.post("/tasks/torrent", status_code=201)
async def create_torrent_task(body: TorrentTaskCreate, request: Request, auth: CsrfSession) -> dict:
    settings = get_settings()
    safe_subdirectory(settings.download_root, body.download_subdir)
    selected = body.selected_indices
    if len(set(selected)) != len(selected) or any(index < 1 for index in selected):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid selected file indices")
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        upload = await db.get(TorrentUpload, body.torrent_id)
        if upload is None:
            raise ApiError(404, "NOT_FOUND", "Torrent upload not found")
        if _utc(upload.expires_at) <= datetime.now(timezone.utc):
            raise ApiError(410, "TORRENT_EXPIRED", "Torrent upload has expired")
        summary = _summary(upload)
        valid = {item["index"] for item in summary["files"]}
        if not set(selected) <= valid:
            raise ApiError(422, "VALIDATION_ERROR", "Selected file index is not in torrent")
        existing = (await db.scalars(select(Task).where(Task.source_fingerprint == upload.infohash).limit(101))).all()
        if existing and not body.allow_duplicates:
            raise ApiError(409, "DUPLICATE_SOURCE", "Torrent already has a task", {"matching_task_ids": [task.id for task in existing]})
        task_id = str(uuid4())
        now = utcnow()
        task = Task(
            id=task_id, kind="general", source_type="torrent", source_url=None,
            source_fingerprint=upload.infohash, title=summary["name"], status="queued",
            download_subdir=body.download_subdir,
            task_directory_key=f"{body.download_subdir}/{task_id}",
            selection_json=json.dumps({"selected_indices": sorted(selected)}),
            options_json="{}", progress_json="{}", warnings_json="[]",
            created_at=now, updated_at=now,
        )
        db.add(task)
        db.add(TaskSource(task_id=task_id, torrent_blob_key=upload.blob_key, source_snapshot_json=json.dumps(summary)))
        position = (await db.scalar(select(func.max(WorkItem.position))) or 0) + 1
        db.add(WorkItem(work_type="download", resource_id=task_id, position=position, available_at=now))
        revision = await bump_queue_revision(db)
        await db.commit()
        await publish_changes(request, [task], revision)
        return task_json(task)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _summary(row: TorrentUpload) -> dict:
    try:
        value = json.loads(row.summary_json)
    except (TypeError, ValueError) as exc:
        raise ApiError(500, "STATE_INVALID", "Stored torrent summary is invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise ApiError(500, "STATE_INVALID", "Stored torrent summary is invalid")
    return value


def _files_page(summary: dict, limit: int, cursor: str | None, search: str | None) -> dict:
    files = summary["files"]
    if search:
        needle = search.strip().casefold()[:200]
        files = [item for item in files if needle in str(item.get("path", "")).casefold()]
    offset = 0
    if cursor:
        try:
            offset = int(cursor)
        except ValueError as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid torrent files cursor") from exc
        if offset < 0:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid torrent files cursor")
    page = files[offset:offset + limit]
    next_cursor = str(offset + limit) if offset + limit < len(files) else None
    return {"items": page, "next_cursor": next_cursor, "total": len(files)}


def torrent_response(row: TorrentUpload, *, limit: int = DEFAULT_FILE_LIMIT) -> dict:
    summary = _summary(row)
    page = _files_page(summary, limit, None, None)
    public_summary = {key: value for key, value in summary.items() if key != "files"}
    return {
        "torrent_id": row.id,
        "summary": public_summary,
        "files": page,
        "files_url": f"/api/v1/torrents/{row.id}/files",
        "created_at": _utc(row.created_at).isoformat(),
        "expires_at": _utc(row.expires_at).isoformat(),
    }


@router.post("/torrents", status_code=201)
async def upload_torrent(
    upload: Annotated[UploadFile, File(alias="file")],
    auth: CsrfSession,
    db: Db,
) -> dict:
    # Read one byte over the limit so a multipart stream cannot bypass the
    # bound by omitting Content-Length or using chunked transfer encoding.
    blob = await upload.read(MAX_UPLOAD_BYTES + 1)
    await upload.close()
    if len(blob) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "TORRENT_TOO_LARGE", "Torrent upload exceeds the 10 MiB limit")
    try:
        parsed = parse_torrent(blob)
    except TorrentParseError as exc:
        raise ApiError(422, "INVALID_TORRENT", str(exc)) from exc

    settings = get_settings()
    torrent_id = str(uuid4())
    try:
        blob_key = persist_torrent_blob(settings.private_root, blob, torrent_id)
    except OSError as exc:
        raise ApiError(507, "STORAGE_UNAVAILABLE", "Unable to persist torrent upload") from exc
    now = utcnow()
    row = TorrentUpload(
        id=torrent_id,
        blob_key=blob_key,
        infohash=parsed.infohash,
        summary_json=summary_json(parsed.summary),
        created_at=now,
        expires_at=now + timedelta(hours=24),
    )
    db.add(row)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            (settings.private_root / blob_key).unlink()
        except OSError:
            pass
        raise
    return torrent_response(row)


@router.get("/torrents/{torrent_id}/files")
async def torrent_files(
    torrent_id: str,
    db: Db,
    auth: AuthSession,
    limit: int = Query(default=DEFAULT_FILE_LIMIT, ge=1, le=100),
    cursor: str | None = None,
    search: str | None = Query(default=None, max_length=200),
) -> dict:
    row = await db.get(TorrentUpload, torrent_id)
    if row is None:
        raise ApiError(404, "NOT_FOUND", "Torrent upload not found")
    if _utc(row.expires_at) <= datetime.now(timezone.utc):
        raise ApiError(410, "TORRENT_EXPIRED", "Torrent upload has expired")
    return _files_page(_summary(row), limit, cursor, search)
