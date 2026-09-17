from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text

from app.api.deps import AuthSession, CsrfSession, Db
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.db.models import DependencyInstallJob, RuntimeControl, Setting, Task, WorkItem
from app.db.session import SessionLocal
from app.services.dependency_install import helper_available

router = APIRouter(tags=["system"])
TOOLS = {"aria2": "aria2c", "yt-dlp": "yt-dlp", "ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "deno": "deno"}
DEFAULTS: dict[str, Any] = {
    "max_active_tasks": 1,
    "download_limit_bps": 0,
    "upload_limit_bps": 0,
    "disk_min_free_bytes": 1073741824,
    "default_video_height": 1080,
    "default_subtitle_languages": ["zh", "en"],
    "allow_auto_subtitles": True,
}


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    max_active_tasks: int | None = Field(default=None, ge=1, le=4)
    download_limit_bps: int | None = Field(default=None, ge=0)
    upload_limit_bps: int | None = Field(default=None, ge=0)
    disk_min_free_bytes: int | None = Field(default=None, ge=0)
    default_video_height: int | None = Field(default=None, ge=144, le=4320)
    default_subtitle_languages: list[str] | None = Field(default=None, max_length=10)
    allow_auto_subtitles: bool | None = None


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str


def install_json(job: DependencyInstallJob) -> dict:
    return {"id": job.id, "action": job.action, "status": job.status,
            "log": job.log_text, "error_code": job.error_code,
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None}


async def read_settings(db: Db) -> tuple[dict[str, Any], int]:
    rows = (await db.scalars(select(Setting))).all()
    values = DEFAULTS.copy()
    revision = 0
    for row in rows:
        if row.key not in DEFAULTS:
            continue
        values[row.key] = json.loads(row.value_json)
        revision = max(revision, row.revision)
    return values, revision


@router.get("/dependencies")
async def dependencies(auth: AuthSession) -> dict:
    async def inspect_tool(name: str, executable: str) -> tuple[str, dict]:
        path = shutil.which(executable)
        if not path:
            return name, {"status": "missing", "version": None}
        try:
            version_flag = "-version" if name in {"ffmpeg", "ffprobe"} else "--version"
            process = await asyncio.create_subprocess_exec(path, version_flag, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except (OSError, asyncio.TimeoutError):
            if "process" in locals() and process.returncode is None:
                process.kill()
                await process.wait()
            return name, {"status": "error", "version": None}
        if process.returncode != 0 or len(output) > 16384:
            return name, {"status": "error", "version": None}
        version = output.decode("utf-8", errors="replace").splitlines()[0][:120] if output else None
        return name, {"status": "available", "version": version}
    results = await asyncio.gather(*(inspect_tool(name, executable) for name, executable in TOOLS.items()))
    return {"items": dict(results), "install_supported": helper_available()}


@router.get("/dependencies/install-jobs")
async def install_jobs(db: Db, auth: AuthSession) -> dict:
    rows = (await db.scalars(select(DependencyInstallJob).order_by(DependencyInstallJob.created_at.desc()).limit(10))).all()
    return {"items": [install_json(row) for row in rows]}


@router.post("/dependencies/install-jobs", status_code=202)
async def create_install_job(body: InstallRequest, auth: CsrfSession) -> dict:
    if body.action not in {"aria2", "ffmpeg"}:
        raise ApiError(422, "INVALID_ACTION", "Only aria2 and ffmpeg can be installed")
    if not helper_available():
        raise ApiError(503, "HELPER_UNAVAILABLE", "Debian 13 installation helper is unavailable")
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        if await db.scalar(select(DependencyInstallJob.id).where(DependencyInstallJob.status.in_({"queued", "running"})).limit(1)):
            raise ApiError(409, "INSTALL_BUSY", "An installation is already pending")
        control = await db.get(RuntimeControl, 1)
        if control is None or control.dispatch_suspended:
            raise ApiError(409, "DISPATCH_SUSPENDED", "Resolve the existing dispatch block first")
        job = DependencyInstallJob(action=body.action, status="queued", log_text="")
        db.add(job)
        control.dispatch_suspended = True
        control.blocked_reason = "dependency_install"
        control.revision += 1
        await db.commit()
        await db.refresh(job)
        return install_json(job)


@router.get("/system/summary")
async def summary(request: Request, db: Db, auth: AuthSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    usage = shutil.disk_usage(settings.download_root)
    scheduler = getattr(request.app.state, "scheduler", None)
    queued = await db.scalar(select(func.count()).select_from(WorkItem))
    control = await db.get(RuntimeControl, 1)
    blocked_reason = control.blocked_reason if control and control.dispatch_suspended else None
    general_waiting = await db.scalar(select(func.count()).select_from(WorkItem).join(Task, WorkItem.resource_id == Task.id).where(Task.kind == "general"))
    if not blocked_reason and scheduler and not scheduler.available and general_waiting:
        blocked_reason = "dependency_missing"
    running = (await db.scalars(select(Task).where(Task.status == "downloading"))).all()
    download_speed = sum(int(json.loads(task.progress_json or "{}").get("speed_bps") or 0) for task in running)
    return {
        "download_speed_bps": download_speed, "upload_speed_bps": 0, "disk_free_bytes": usage.free,
        "memory_available_bytes": None, "running_tasks": len(scheduler.active) + len(scheduler.video_active) if scheduler else 0,
        "queued_items": queued, "blocked_reason": blocked_reason,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/queue/resume-dispatch")
async def resume_dispatch(db: Db, auth: CsrfSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    values, _ = await read_settings(db)
    if shutil.disk_usage(settings.download_root).free < values["disk_min_free_bytes"]:
        raise ApiError(409, "DISK_LOW", "Free disk space remains below the configured threshold")
    async with SessionLocal() as write_db:
        await write_db.execute(text("BEGIN IMMEDIATE"))
        if await write_db.scalar(select(DependencyInstallJob.id).where(DependencyInstallJob.status.in_({"queued", "running"})).limit(1)):
            raise ApiError(409, "INSTALL_BUSY", "Dispatch cannot resume during dependency installation")
        control = await write_db.get(RuntimeControl, 1)
        if control is None:
            control = RuntimeControl(id=1, dispatch_suspended=False, revision=1)
            write_db.add(control)
        else:
            control.dispatch_suspended = False
            control.blocked_reason = None
            control.revision += 1
        await write_db.commit()
        return {"dispatch_suspended": control.dispatch_suspended, "blocked_reason": control.blocked_reason, "revision": control.revision}


@router.get("/settings")
async def get_editable_settings(db: Db, auth: AuthSession, settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    values, revision = await read_settings(db)
    return {"editable": values, "read_only": {"download_root": str(settings.download_root)}, "revision": revision}


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, auth: CsrfSession) -> dict:
    # Use a fresh connection so auth's read transaction cannot race the write lock.
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        _, current_revision = await read_settings(db)
        if body.revision != current_revision:
            raise ApiError(409, "REVISION_CONFLICT", "Settings changed; refresh and try again")
        new_revision = current_revision + 1
        for key, value in body.model_dump(exclude={"revision"}, exclude_none=True).items():
            row = await db.get(Setting, key)
            if row is None:
                db.add(Setting(key=key, value_json=json.dumps(value), revision=new_revision))
            else:
                row.value_json, row.revision = json.dumps(value), new_revision
        await db.commit()
        values, _ = await read_settings(db)
    return {"editable": values, "revision": new_revision, "effects": {"active_tasks": "deferred"}}
