"""Single-worker, restart-safe video metadata analysis."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta

from sqlalchemy import delete, select, update

from app.core.config import Settings
from app.core.errors import ApiError
from app.db.models import CookieProfile, FileRecord, Task, VideoAnalysisItem, VideoAnalysisJob, utcnow
from app.db.session import SessionLocal
from app.services.cookies import cookie_blob_path
from app.services.video import resolve_collection_page, resolve_video

logger = logging.getLogger("grabbit.video_analysis")
ANALYSIS_RETENTION = timedelta(hours=24)


class VideoAnalysisWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runner: asyncio.Task | None = None
        self.active_job_id: str | None = None
        self.active_resolve: asyncio.Task | None = None
        self.last_expire_check = 0.0

    def cancel_running(self, job_id: str) -> None:
        if self.active_job_id == job_id and self.active_resolve is not None:
            self.active_resolve.cancel()

    async def expire_old(self) -> None:
        if time.monotonic() - self.last_expire_check < 60:
            return
        self.last_expire_check = time.monotonic()
        async with SessionLocal() as db:
            cutoff = utcnow() - ANALYSIS_RETENTION
            await db.execute(delete(VideoAnalysisItem).where(VideoAnalysisItem.analysis_job_id.in_(select(VideoAnalysisJob.id).where(VideoAnalysisJob.created_at < cutoff))))
            await db.execute(update(VideoAnalysisJob).where(
                VideoAnalysisJob.created_at < cutoff,
                VideoAnalysisJob.status != "expired",
            ).values(status="expired", result_json=None, error_code="ANALYSIS_EXPIRED", updated_at=utcnow()))
            await db.commit()

    async def start(self) -> None:
        await self.expire_old()
        async with SessionLocal() as db:
            await db.execute(update(VideoAnalysisJob).where(VideoAnalysisJob.status == "running").values(status="queued", updated_at=utcnow()))
            await db.commit()
        self.runner = asyncio.create_task(self._run(), name="grabbit-video-analysis")

    async def close(self) -> None:
        if self.runner is not None:
            self.runner.cancel()
            try:
                await self.runner
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("video analysis worker failed")
            await asyncio.sleep(1)

    async def tick(self) -> None:
        await self.expire_old()
        async with SessionLocal() as db:
            busy_task = await db.scalar(select(Task.id).where((Task.status.in_(("resolving", "downloading", "postprocessing"))) | (Task.phase == "subtitle_retry")).limit(1))
            busy_probe = await db.scalar(select(FileRecord.id).where(FileRecord.probe_status == "running").limit(1))
            if busy_task is not None or busy_probe is not None:
                return
            job = await db.scalar(select(VideoAnalysisJob).where(VideoAnalysisJob.status == "queued").order_by(VideoAnalysisJob.created_at, VideoAnalysisJob.id).limit(1))
            if job is None:
                return
            job.status = "running"
            job.updated_at = utcnow()
            await db.commit()
            job_id, url, site, profile_id, page_start = job.id, job.url, job.site_key, job.cookie_profile_id, job.page_start
        try:
            cookie_file = None
            if profile_id:
                async with SessionLocal() as db:
                    profile = await db.get(CookieProfile, profile_id)
                    if profile is None or profile.site_key != site:
                        raise ApiError(422, "COOKIE_UNAVAILABLE", "Cookie profile is no longer available")
                    cookie_file = cookie_blob_path(self.settings.private_root, profile.secret_file_key)
                    if not cookie_file.is_file() or cookie_file.is_symlink():
                        raise ApiError(507, "COOKIE_UNAVAILABLE", "Cookie content is unavailable")
            self.active_job_id = job_id
            async def analyze():
                collection = await resolve_collection_page(url, site, cookie_file, page_start)
                return collection if collection is not None else await resolve_video(url, site, cookie_file)
            self.active_resolve = asyncio.create_task(analyze(), name=f"grabbit-analysis-{job_id}")
            result = await self.active_resolve
        except ApiError as exc:
            status, result_json, error_code = "failed", None, exc.code
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                # A stopped service leaves the job running; start() requeues it.
                raise
            status, result_json, error_code = "cancelled", None, None
        except Exception:
            logger.exception("video analysis failed unexpectedly")
            status, result_json, error_code = "failed", None, "ANALYSIS_FAILED"
        else:
            status, result_json, error_code = "completed", json.dumps({key: value for key, value in result.items() if key != "items"}), None
        finally:
            self.active_job_id = None
            self.active_resolve = None
        async with SessionLocal() as db:
            job = await db.get(VideoAnalysisJob, job_id)
            if job is not None and job.status == "running":
                if status == "completed" and result.get("collection"):
                    if page_start > 1 and job.result_json:
                        previous = json.loads(job.result_json)
                        result["title"] = previous.get("title") or result["title"]
                        result_json = json.dumps({key: value for key, value in result.items() if key != "items"})
                    for item in result["items"]:
                        exists = await db.scalar(select(VideoAnalysisItem.id).where(VideoAnalysisItem.analysis_job_id == job_id, VideoAnalysisItem.ordinal == item["ordinal"]))
                        if exists is None:
                            db.add(VideoAnalysisItem(analysis_job_id=job_id, **item))
                    job.next_index = result["next_index"]
                job.status, job.result_json, job.error_code = status, result_json, error_code
                job.updated_at = utcnow()
                await db.commit()
