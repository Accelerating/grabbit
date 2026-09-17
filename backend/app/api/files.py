from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.api.deps import AuthSession, CsrfSession, Db
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.paths import safe_subdirectory
from app.db.models import FileOperation, FileRecord, Task, utcnow
from app.db.session import SessionLocal

router = APIRouter(prefix="/files", tags=["files"])
directory_router = APIRouter(tags=["files"])
INTERNAL_SUFFIXES = {".aria2", ".part"}
INTERNAL_TORRENT_NAME = re.compile(r"^[0-9a-fA-F]{40}\.torrent$")
PREVIEW_KEY = secrets.token_bytes(32)
BUSY_TASK_STATES = {"queued", "resolving", "awaiting_selection", "downloading", "postprocessing", "retry_wait", "paused"}


def _safe_directory(root: Path, relative: str) -> Path:
    if relative in {"", "."}:
        return root.resolve()
    return safe_subdirectory(root, relative)


def _safe_file(root: Path, relative: str) -> Path:
    path = safe_subdirectory(root, relative)
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ApiError(404, "FILE_MISSING", "File is missing")
    return path


def file_json(record: FileRecord) -> dict:
    mime_type = record.mime_type or mimetypes.guess_type(record.display_name)[0] or "application/octet-stream"
    kind = "video" if mime_type.startswith("video/") else "audio" if mime_type.startswith("audio/") else record.kind
    return {
        "id": record.id, "name": record.display_name, "relative_path": record.relative_path,
        "size_bytes": record.size_bytes, "mtime_ns": record.mtime_ns,
        "mime_type": mime_type, "kind": kind,
        "availability": record.availability, "is_complete": record.is_complete,
        "task_id": record.task_id,
        "probe_status": record.probe_status,
        "media": json.loads(record.media_json) if record.media_json and record.media_mtime_ns == record.mtime_ns else None,
    }


def operation_json(operation: FileOperation) -> dict:
    targets = json.loads(operation.target_snapshot_json)
    return {"id": operation.id, "kind": operation.kind, "status": operation.status,
            "total": len(targets), "items": json.loads(operation.results_json),
            "created_at": operation.created_at.isoformat(),
            "finished_at": operation.finished_at.isoformat() if operation.finished_at else None}


class FileDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_ids: list[str] = Field(min_length=1, max_length=100)


class DirectoryDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directory: str = Field(min_length=1, max_length=1024)


class FileDeletionConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(min_length=1, max_length=50000)
    confirmed: bool


class DirectoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent: str = ""
    name: str = Field(min_length=1, max_length=255)


@directory_router.post("/directories", status_code=201)
async def create_directory(body: DirectoryCreate, auth: CsrfSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    name = body.name.strip()
    if name in {"", ".", ".."} or name.startswith(".") or any(char in name for char in ("/", "\\", "\x00")) or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid directory name")
    parent = _safe_directory(settings.download_root, body.parent)
    if not parent.is_dir() or parent.is_symlink():
        raise ApiError(404, "NOT_FOUND", "Parent directory not found")
    relative = f"{body.parent.rstrip('/')}/{name}".lstrip("/")
    target = safe_subdirectory(settings.download_root, relative)
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise ApiError(409, "ALREADY_EXISTS", "Directory already exists") from exc
    except OSError as exc:
        raise ApiError(507, "STORAGE_UNAVAILABLE", "Could not create directory") from exc
    return {"relative_path": relative, "name": name}


def _preview_token(snapshot: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(snapshot, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(PREVIEW_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _read_preview_token(token: str) -> dict:
    try:
        payload, signature = token.rsplit(".", 1)
        expected = hmac.new(PREVIEW_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        snapshot = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error, UnicodeDecodeError, TypeError) as exc:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid deletion preview") from exc
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("files"), list) or not isinstance(snapshot.get("expires_at"), int):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid deletion preview")
    if snapshot["expires_at"] < int(time.time()):
        raise ApiError(409, "PREVIEW_EXPIRED", "Deletion preview expired; preview again")
    return snapshot


def _snapshot_directory(root: Path, relative: str) -> list[dict]:
    folder = safe_subdirectory(root, relative)
    if not folder.is_dir() or folder.is_symlink() or folder == root.resolve():
        raise ApiError(404, "NOT_FOUND", "Directory not found")
    files: list[dict] = []
    directories: list[dict] = []
    pending = [folder]
    while pending:
        current = pending.pop()
        if len(files) + len(directories) >= 100:
            raise ApiError(409, "DIRECTORY_TOO_LARGE", "Directory deletion is limited to 100 entries")
        info = current.stat(follow_symlinks=False)
        directories.append({"type": "directory", "id": f"directory:{current.relative_to(root).as_posix()}",
                            "relative_path": current.relative_to(root).as_posix(), "dev": info.st_dev, "ino": info.st_ino})
        try:
            with os.scandir(current) as scan:
                entries = list(scan)
        except OSError as exc:
            raise ApiError(409, "DIRECTORY_CHANGED", "Directory cannot be read") from exc
        for entry in sorted(entries, key=lambda entry: entry.name):
            if entry.is_symlink():
                raise ApiError(409, "DIRECTORY_UNSAFE", "Directory contains a symbolic link")
            item_path = current / entry.name
            if entry.is_dir(follow_symlinks=False):
                pending.append(item_path)
            elif entry.is_file(follow_symlinks=False):
                info = entry.stat(follow_symlinks=False)
                files.append({"type": "file", "id": f"path:{item_path.relative_to(root).as_posix()}",
                              "name": entry.name, "relative_path": item_path.relative_to(root).as_posix(),
                              "size_bytes": info.st_size, "mtime_ns": info.st_mtime_ns,
                              "dev": info.st_dev, "ino": info.st_ino})
            else:
                raise ApiError(409, "DIRECTORY_UNSAFE", "Directory contains an unsupported entry")
            if len(files) + len(directories) + len(pending) > 100:
                raise ApiError(409, "DIRECTORY_TOO_LARGE", "Directory deletion is limited to 100 entries")
    return files + sorted(directories, key=lambda item: item["relative_path"].count("/"), reverse=True)


async def _assert_directory_not_busy(db, relative: str) -> None:
    busy = (await db.scalars(select(Task).where(Task.status.in_(BUSY_TASK_STATES)))).all()
    if any(task.task_directory_key == relative or task.task_directory_key.startswith(relative + "/") or relative.startswith(task.task_directory_key + "/") for task in busy):
        raise ApiError(409, "DIRECTORY_IN_USE", "Directory is occupied by an active task")


@directory_router.post("/directories/deletion-preview")
async def preview_directory_deletion(body: DirectoryDeletionRequest, db: Db, auth: CsrfSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    await _assert_directory_not_busy(db, body.directory)
    items = _snapshot_directory(settings.download_root.resolve(), body.directory)
    expires_at = int(time.time()) + 300
    snapshot = {"kind": "delete_directory", "directory": body.directory, "files": items, "expires_at": expires_at}
    preview_id = _preview_token(snapshot)
    if len(preview_id) > 50000:
        raise ApiError(409, "DIRECTORY_TOO_LARGE", "Directory deletion preview is too large")
    return {"preview_id": preview_id, "directory": body.directory,
            "file_count": sum(item["type"] == "file" for item in items),
            "directory_count": sum(item["type"] == "directory" for item in items),
            "total_bytes": sum(item.get("size_bytes", 0) for item in items), "expires_at": expires_at}


@router.post("/deletion-preview")
async def preview_file_deletion(body: FileDeletionRequest, db: Db, auth: CsrfSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    if len(set(body.file_ids)) != len(body.file_ids):
        raise ApiError(422, "VALIDATION_ERROR", "Duplicate file IDs")
    files = []
    for file_id in body.file_ids:
        record = await db.get(FileRecord, file_id)
        if record is None:
            raise ApiError(404, "NOT_FOUND", "File not found")
        if record.task_id:
            task = await db.get(Task, record.task_id)
            if task is None or task.status in BUSY_TASK_STATES:
                raise ApiError(409, "FILE_IN_USE", "File belongs to an active or missing task")
        path = _safe_file(settings.download_root, record.relative_path)
        stat = path.stat()
        files.append({"id": record.id, "name": record.display_name, "relative_path": record.relative_path,
                      "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "dev": stat.st_dev, "ino": stat.st_ino})
    expires_at = int(time.time()) + 300
    return {"preview_id": _preview_token({"files": files, "expires_at": expires_at}), "files": [{"id": file["id"], "name": file["name"], "size_bytes": file["size_bytes"]} for file in files], "total_bytes": sum(file["size_bytes"] for file in files), "expires_at": expires_at}


@router.post("/delete", status_code=202)
async def delete_files(body: FileDeletionConfirm, auth: CsrfSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    if not body.confirmed:
        raise ApiError(422, "VALIDATION_ERROR", "Explicit confirmation is required")
    snapshot = _read_preview_token(body.preview_id)
    files = snapshot["files"]
    is_directory = snapshot.get("kind") == "delete_directory"
    if not 1 <= len(files) <= 100:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid deletion preview")
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        preview_hash = hashlib.sha256(body.preview_id.encode()).hexdigest()
        existing = await db.scalar(select(FileOperation).where(FileOperation.preview_hash == preview_hash))
        if existing is not None:
            return operation_json(existing)
        if is_directory:
            relative = snapshot.get("directory")
            if not isinstance(relative, str) or not relative:
                raise ApiError(422, "VALIDATION_ERROR", "Invalid deletion preview")
            await _assert_directory_not_busy(db, relative)
            if _snapshot_directory(settings.download_root.resolve(), relative) != files:
                raise ApiError(409, "PREVIEW_CHANGED", "Directory changed; preview again")
        else:
            for item in files:
                if not isinstance(item, dict) or not all(key in item for key in ("id", "relative_path", "size_bytes", "mtime_ns", "dev", "ino")):
                    raise ApiError(422, "VALIDATION_ERROR", "Invalid deletion preview")
                record = await db.get(FileRecord, item["id"])
                if record is None or record.relative_path != item["relative_path"]:
                    raise ApiError(409, "PREVIEW_CHANGED", "File list changed; preview again")
                if record.task_id:
                    task = await db.get(Task, record.task_id)
                    if task is None or task.status in BUSY_TASK_STATES:
                        raise ApiError(409, "FILE_IN_USE", "File belongs to an active or missing task")
                path = _safe_file(settings.download_root, record.relative_path)
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino) != (item["size_bytes"], item["mtime_ns"], item["dev"], item["ino"]):
                    raise ApiError(409, "PREVIEW_CHANGED", "File changed; preview again")
        operation = FileOperation(preview_hash=preview_hash, kind="delete_directory" if is_directory else "delete_files", status="queued",
                                  target_snapshot_json=json.dumps(files), results_json="[]")
        db.add(operation)
        await db.commit()
        return operation_json(operation)


@router.get("/operations/{operation_id}")
async def get_file_operation(operation_id: str, db: Db, auth: AuthSession) -> dict:
    operation = await db.get(FileOperation, operation_id)
    if operation is None:
        raise ApiError(404, "NOT_FOUND", "File operation not found")
    return operation_json(operation)


async def subtitle_records(db: Db, record: FileRecord) -> list[FileRecord]:
    if not record.task_id:
        return []
    prefix = Path(record.relative_path).stem + "."
    rows = (await db.scalars(select(FileRecord).where(FileRecord.task_id == record.task_id).order_by(FileRecord.relative_path))).all()
    return [row for row in rows if row.relative_path.startswith(str(Path(record.relative_path).parent / prefix)) and row.relative_path.lower().endswith(".vtt")][:20]


@router.get("")
async def browse_files(
    db: Db, auth: AuthSession, settings: Annotated[Settings, Depends(get_settings)],
    directory: str = "", query: str = "", cursor: str | None = None,
    limit: int = Query(default=30, ge=1, le=100),
) -> dict:
    root = settings.download_root.resolve()
    folder = _safe_directory(root, directory)
    if not folder.is_dir() or folder.is_symlink():
        raise ApiError(404, "FILE_MISSING", "Directory is missing")
    entries = []
    try:
        with os.scandir(folder) as scan:
            for entry in scan:
                if entry.is_symlink() or entry.name.startswith(".") or Path(entry.name).suffix in INTERNAL_SUFFIXES or INTERNAL_TORRENT_NAME.fullmatch(entry.name):
                    continue
                if query and query.casefold() not in entry.name.casefold():
                    continue
                if not (entry.is_dir(follow_symlinks=False) or entry.is_file(follow_symlinks=False)):
                    continue
                entries.append(entry)
    except OSError as exc:
        raise ApiError(404, "FILE_MISSING", "Directory cannot be read") from exc
    entries.sort(key=lambda entry: (not entry.is_dir(follow_symlinks=False), entry.name.casefold(), entry.name))
    if cursor:
        # Opaque cursor is the previous item name; recheck against the current directory.
        try:
            start = next(index + 1 for index, entry in enumerate(entries) if entry.name == cursor)
        except StopIteration as exc:
            raise ApiError(422, "VALIDATION_ERROR", "Invalid directory cursor") from exc
    else:
        start = 0
    selected = entries[start : start + limit + 1]
    has_more = len(selected) > limit
    selected = selected[:limit]
    items = []
    for entry in selected:
        relative = (folder / entry.name).relative_to(root).as_posix()
        try:
            stat = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if entry.is_dir(follow_symlinks=False):
            items.append({"type": "directory", "name": entry.name, "relative_path": relative, "mtime_ns": stat.st_mtime_ns})
            continue
        record = await db.scalar(select(FileRecord).where(FileRecord.relative_path == relative))
        mime_type = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
        if record is None:
            record = FileRecord(
                relative_path=relative, display_name=entry.name, size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns, mime_type=mime_type,
                kind="video" if mime_type.startswith("video/") else "audio" if mime_type.startswith("audio/") else "other",
                availability="present", is_complete=True,
            )
            db.add(record)
            await db.flush()
        elif record.size_bytes != stat.st_size or record.mtime_ns != stat.st_mtime_ns or record.availability != "present":
            record.size_bytes = stat.st_size
            record.mtime_ns = stat.st_mtime_ns
            record.availability = "present"
            if record.media_mtime_ns != stat.st_mtime_ns:
                record.probe_status = "unprobed"
                record.media_json = None
                record.media_mtime_ns = None
            record.updated_at = utcnow()
        items.append({"type": "file", **file_json(record)})
    await db.commit()
    return {"items": items, "next_cursor": selected[-1].name if has_more and selected else None, "directory": directory}


@router.get("/{file_id}")
async def file_detail(file_id: str, db: Db, auth: AuthSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    record = await db.get(FileRecord, file_id)
    if record is None:
        raise ApiError(404, "NOT_FOUND", "File not found")
    try:
        stat = _safe_file(settings.download_root, record.relative_path).stat()
        if record.mtime_ns != stat.st_mtime_ns or record.size_bytes != stat.st_size:
            record.mtime_ns, record.size_bytes = stat.st_mtime_ns, stat.st_size
            record.probe_status, record.media_json, record.media_mtime_ns = "unprobed", None, None
            await db.commit()
    except ApiError:
        record.availability = "missing"
        await db.commit()
    return file_json(record)


@router.post("/{file_id}/probe", status_code=202)
async def queue_file_probe(file_id: str, db: Db, auth: CsrfSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    record = await db.get(FileRecord, file_id)
    if record is None or not record.is_complete:
        raise ApiError(404, "FILE_MISSING", "File is unavailable")
    mime_type = record.mime_type or mimetypes.guess_type(record.display_name)[0] or ""
    if not mime_type.startswith(("audio/", "video/")):
        raise ApiError(422, "NOT_MEDIA", "File is not audio or video")
    stat = _safe_file(settings.download_root, record.relative_path).stat()
    if record.mtime_ns != stat.st_mtime_ns or record.size_bytes != stat.st_size:
        record.mtime_ns, record.size_bytes = stat.st_mtime_ns, stat.st_size
        record.probe_status, record.media_json, record.media_mtime_ns = "unprobed", None, None
    if record.probe_status in {"unprobed", "failed"}:
        record.probe_status = "queued"
    await db.commit()
    return file_json(record)


@router.get("/{file_id}/subtitles")
async def list_subtitles(file_id: str, db: Db, auth: AuthSession) -> dict:
    record = await db.get(FileRecord, file_id)
    if record is None or not record.is_complete:
        raise ApiError(404, "FILE_MISSING", "File is unavailable")
    rows = await subtitle_records(db, record)
    return {"items": [{"id": row.id, "label": row.display_name, "language": row.display_name.removesuffix(".vtt").rsplit(".", 1)[-1][:16]} for row in rows]}


@router.get("/{file_id}/subtitles/{subtitle_id}")
async def subtitle_content(file_id: str, subtitle_id: str, db: Db, auth: AuthSession, settings: Annotated[Settings, Depends(get_settings)]):
    record = await db.get(FileRecord, file_id)
    if record is None or not record.is_complete:
        raise ApiError(404, "FILE_MISSING", "File is unavailable")
    subtitle = next((row for row in await subtitle_records(db, record) if row.id == subtitle_id), None)
    if subtitle is None or not subtitle.is_complete:
        raise ApiError(404, "FILE_MISSING", "Subtitle is unavailable")
    path = _safe_file(settings.download_root, subtitle.relative_path)
    return FileResponse(path, media_type="text/vtt; charset=utf-8", content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.api_route("/{file_id}/content", methods=["GET", "HEAD"])
async def file_content(
    file_id: str, request: Request, db: Db, auth: AuthSession,
    settings: Annotated[Settings, Depends(get_settings)],
    disposition: str = "attachment",
):
    record = await db.get(FileRecord, file_id)
    if record is None or not record.is_complete:
        raise ApiError(404, "FILE_MISSING", "File is unavailable")
    path = _safe_file(settings.download_root, record.relative_path)
    if disposition not in {"inline", "attachment"}:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid disposition")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if disposition == "inline" and not mime_type.startswith(("audio/", "video/")):
        disposition = "attachment"
    return FileResponse(
        path, filename=path.name, media_type=mime_type,
        content_disposition_type=disposition,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
    )
