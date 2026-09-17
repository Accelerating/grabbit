"""Authenticated video pre-analysis; task creation is a later stage."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select, text

from app.api.deps import AuthSession, CsrfSession, Db
from app.api.tasks import bump_queue_revision, publish_changes, task_json
from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.paths import safe_subdirectory
from app.db.models import CookieProfile, Task, TaskGroup, VideoAnalysisItem, VideoAnalysisJob, WorkItem, utcnow
from app.db.session import SessionLocal
from app.services.cookies import cookie_blob_path
from app.services.video import resolve_video, video_site

router = APIRouter(prefix="/video", tags=["video"])


def analysis_json(job: VideoAnalysisJob) -> dict:
    return {"id": job.id, "url": job.url, "cookie_profile_id": job.cookie_profile_id,
            "status": job.status, "result": json.loads(job.result_json) if job.result_json else None,
            "error_code": job.error_code, "next_index": job.next_index,
            "created_at": job.created_at.isoformat(), "updated_at": job.updated_at.isoformat()}


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    cookie_profile_id: str | None = None


@router.post("/analyses", status_code=202)
async def create_analysis(body: ResolveRequest, db: Db, auth: CsrfSession) -> dict:
    site = video_site(body.url)
    if body.cookie_profile_id:
        profile = await db.get(CookieProfile, body.cookie_profile_id)
        if profile is None or profile.site_key != site:
            raise ApiError(422, "COOKIE_SITE_MISMATCH", "Cookie profile does not belong to this site")
    job = VideoAnalysisJob(url=body.url.strip(), site_key=site, cookie_profile_id=body.cookie_profile_id,
                           status="queued", created_at=utcnow(), updated_at=utcnow())
    db.add(job)
    await db.commit()
    return analysis_json(job)


@router.get("/analyses/{job_id}")
async def get_analysis(job_id: str, db: Db, auth: AuthSession) -> dict:
    job = await db.get(VideoAnalysisJob, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "Video analysis was not found")
    return analysis_json(job)


@router.get("/analyses/{job_id}/items")
async def analysis_items(job_id: str, db: Db, auth: AuthSession, cursor: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100)) -> dict:
    job = await db.get(VideoAnalysisJob, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "Video analysis was not found")
    rows = (await db.scalars(select(VideoAnalysisItem).where(VideoAnalysisItem.analysis_job_id == job_id, VideoAnalysisItem.ordinal > cursor).order_by(VideoAnalysisItem.ordinal).limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {"items": [{"id": row.id, "ordinal": row.ordinal, "title": row.title, "duration_seconds": row.duration_seconds, "available": bool(row.source_url)} for row in rows],
            "next_cursor": rows[-1].ordinal if has_more and rows else None, "next_source_index": job.next_index}


@router.post("/analyses/{job_id}/next-page", status_code=202)
async def load_analysis_page(job_id: str, db: Db, auth: CsrfSession) -> dict:
    job = await db.get(VideoAnalysisJob, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "Video analysis was not found")
    if job.status != "completed" or job.next_index is None or not json.loads(job.result_json or "{}").get("collection"):
        raise ApiError(409, "STATE_CONFLICT", "No more collection entries are available")
    job.page_start = job.next_index
    job.next_index = None
    job.status = "queued"
    job.updated_at = utcnow()
    await db.commit()
    return analysis_json(job)


class CollectionTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(min_length=1, max_length=500)
    cookie_profile_id: str | None = None
    mode: str = "video"
    max_height: int = Field(default=1080, ge=144, le=4320)
    subtitle_languages: list[str] = Field(default_factory=lambda: ["zh.*", "en.*"], max_length=10)
    download_subdir: str = Field(default="videos", min_length=1, max_length=255)
    allow_duplicates: bool = False


@router.post("/analyses/{job_id}/tasks", status_code=201)
async def create_collection_tasks(job_id: str, body: CollectionTaskRequest, request: Request, auth: CsrfSession) -> dict:
    if len(set(body.item_ids)) != len(body.item_ids) or body.mode not in {"video", "audio"}:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid collection selection")
    if len(set(body.subtitle_languages)) != len(body.subtitle_languages) or any(not re.fullmatch(r"[A-Za-z0-9-]{2,32}|(?:zh|en)\.\*", item) for item in body.subtitle_languages):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid subtitle languages")
    safe_subdirectory(get_settings().download_root, body.download_subdir)
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        job = await db.get(VideoAnalysisJob, job_id)
        if job is None or job.status != "completed" or not json.loads(job.result_json or "{}").get("collection"):
            raise ApiError(409, "STATE_CONFLICT", "Collection analysis is not ready")
        profile = None
        if body.cookie_profile_id:
            profile = await db.get(CookieProfile, body.cookie_profile_id)
            if profile is None or profile.site_key != job.site_key:
                raise ApiError(422, "COOKIE_SITE_MISMATCH", "Cookie profile does not belong to this site")
        rows = (await db.scalars(select(VideoAnalysisItem).where(VideoAnalysisItem.analysis_job_id == job_id, VideoAnalysisItem.id.in_(body.item_ids)).order_by(VideoAnalysisItem.ordinal))).all()
        if len(rows) != len(body.item_ids) or any(not row.source_url or video_site(row.source_url) != job.site_key for row in rows):
            raise ApiError(422, "VALIDATION_ERROR", "Collection contains unavailable entries")
        fingerprints = [hashlib.sha256(row.source_url.encode()).hexdigest() for row in rows]
        if not body.allow_duplicates:
            existing = (await db.scalars(select(Task.id).where(Task.kind == "video", Task.source_fingerprint.in_(fingerprints)))).all()
            if existing or len(set(fingerprints)) != len(fingerprints):
                raise ApiError(409, "DUPLICATE_SOURCE", "Video source already exists", {"matching_task_ids": existing})
        summary = json.loads(job.result_json)
        group = TaskGroup(title=summary.get("title") or "Collection", source_url=job.url, site_key=job.site_key)
        db.add(group)
        await db.flush()
        position = (await db.scalar(select(func.max(WorkItem.position))) or 0) + 1
        now = utcnow()
        tasks = []
        for row, fingerprint in zip(rows, fingerprints):
            task_id = str(uuid4())
            task = Task(id=task_id, kind="video", source_type="video", source_url=row.source_url,
                        source_fingerprint=fingerprint, title=row.title, status="queued", group_id=group.id,
                        download_subdir=body.download_subdir, task_directory_key=f"{body.download_subdir}/{task_id}",
                        cookie_profile_id=profile.id if profile else None, cookie_name_snapshot=profile.name if profile else None,
                        selection_json="{}", options_json=json.dumps({"mode": body.mode, "max_height": body.max_height, "subtitle_languages": body.subtitle_languages}),
                        progress_json="{}", warnings_json="[]", created_at=now, updated_at=now)
            db.add(task)
            db.add(WorkItem(work_type="video", resource_id=task_id, position=position))
            position += 1
            tasks.append(task)
        revision = await bump_queue_revision(db)
        await db.commit()
        await publish_changes(request, tasks, revision)
        return {"group_id": group.id, "items": [task_json(task) for task in tasks]}


@router.post("/analyses/{job_id}/cancel")
async def cancel_analysis(job_id: str, request: Request, db: Db, auth: CsrfSession) -> dict:
    job = await db.get(VideoAnalysisJob, job_id)
    if job is None:
        raise ApiError(404, "NOT_FOUND", "Video analysis was not found")
    if job.status in {"queued", "running"}:
        job.status = "cancelled"
        job.updated_at = utcnow()
        await db.commit()
        worker = getattr(request.app.state, "analysis_worker", None)
        if worker is not None:
            worker.cancel_running(job_id)
    return analysis_json(job)


@router.post("/resolve")
async def resolve(body: ResolveRequest, db: Db, auth: CsrfSession) -> dict:
    site = video_site(body.url)
    cookie_file = None
    if body.cookie_profile_id is not None:
        profile = await db.get(CookieProfile, body.cookie_profile_id)
        if profile is None or profile.site_key != site:
            raise ApiError(422, "COOKIE_SITE_MISMATCH", "Cookie profile does not belong to this site")
        cookie_file = cookie_blob_path(get_settings().private_root, profile.secret_file_key)
        if not cookie_file.is_file() or cookie_file.is_symlink():
            raise ApiError(507, "STORAGE_UNAVAILABLE", "Cookie content is unavailable")
    return await resolve_video(body.url.strip(), site, cookie_file)


class VideoCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=8192)
    title: str = Field(min_length=1, max_length=255)
    cookie_profile_id: str | None = None
    mode: str = "video"
    max_height: int = Field(default=1080, ge=144, le=4320)
    download_subdir: str = Field(default="videos", min_length=1, max_length=255)
    allow_duplicates: bool = False
    subtitle_languages: list[str] = Field(default_factory=lambda: ["zh.*", "en.*"], max_length=10)


@router.post("/tasks", status_code=201)
async def create_video_task(body: VideoCreateRequest, request: Request, auth: CsrfSession) -> dict:
    site = video_site(body.url)
    if body.mode not in {"video", "audio"}:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid video mode")
    if len(set(body.subtitle_languages)) != len(body.subtitle_languages) or any(not re.fullmatch(r"[A-Za-z0-9-]{2,32}|(?:zh|en)\.\*", item) for item in body.subtitle_languages):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid subtitle languages")
    safe_subdirectory(get_settings().download_root, body.download_subdir)
    fingerprint = hashlib.sha256(body.url.strip().encode()).hexdigest()
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        profile = None
        if body.cookie_profile_id:
            profile = await db.get(CookieProfile, body.cookie_profile_id)
            if profile is None or profile.site_key != site:
                raise ApiError(422, "COOKIE_SITE_MISMATCH", "Cookie profile does not belong to this site")
        if not body.allow_duplicates:
            existing = await db.scalar(select(Task.id).where(Task.kind == "video", Task.source_fingerprint == fingerprint).limit(1))
            if existing is not None:
                raise ApiError(409, "DUPLICATE_SOURCE", "Video source already exists", {"matching_task_ids": [existing]})
        task_id = str(uuid4())
        now = utcnow()
        task = Task(
            id=task_id, kind="video", source_type="video", source_url=body.url.strip(),
            source_fingerprint=fingerprint, title=body.title.strip(), status="queued",
            download_subdir=body.download_subdir, task_directory_key=f"{body.download_subdir}/{task_id}",
            cookie_profile_id=profile.id if profile else None,
            cookie_name_snapshot=profile.name if profile else None,
            selection_json="{}", options_json=json.dumps({"mode": body.mode, "max_height": body.max_height, "subtitle_languages": body.subtitle_languages}),
            progress_json="{}", warnings_json="[]", created_at=now, updated_at=now,
        )
        db.add(task)
        position = (await db.scalar(select(func.max(WorkItem.position))) or 0) + 1
        db.add(WorkItem(work_type="video", resource_id=task_id, position=position, available_at=now))
        revision = await bump_queue_revision(db)
        await db.commit()
        await publish_changes(request, [task], revision)
        return task_json(task)
