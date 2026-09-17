"""Bounded, restart-safe ffprobe work for file details."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal

from sqlalchemy import select, update

from app.core.config import Settings
from app.core.paths import safe_subdirectory
from app.db.models import FileRecord, Task, VideoAnalysisJob, utcnow
from app.db.session import SessionLocal

logger = logging.getLogger("grabbit.media_probe")


async def inspect_media(path) -> dict | None:
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    process = await asyncio.create_subprocess_exec(
        executable, "-v", "error", "-show_entries",
        "format=duration,format_name:stream=codec_type,codec_name,width,height,channels,sample_rate",
        "-of", "json", str(path), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    try:
        output = await asyncio.wait_for(process.stdout.read(262145), timeout=20)
        if len(output) > 262144:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            return None
        await asyncio.wait_for(process.wait(), timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        raise
    if process.returncode != 0:
        return None
    try:
        raw = json.loads(output)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    streams = raw.get("streams")
    if not isinstance(streams, list):
        return None
    normalized = []
    for stream in streams[:32]:
        if not isinstance(stream, dict) or stream.get("codec_type") not in {"audio", "video"}:
            continue
        normalized.append({key: stream[key] for key in ("codec_type", "codec_name", "width", "height", "channels", "sample_rate") if key in stream and isinstance(stream[key], (str, int))})
    if not normalized:
        return None
    fmt = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    try:
        duration = float(fmt.get("duration"))
        if not 0 <= duration < 10**9:
            duration = None
    except (TypeError, ValueError):
        duration = None
    return {"duration_seconds": duration, "format_name": str(fmt.get("format_name") or "")[:128], "streams": normalized}


class MediaProbeWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runner: asyncio.Task | None = None

    async def start(self) -> None:
        async with SessionLocal() as db:
            await db.execute(update(FileRecord).where(FileRecord.probe_status == "running").values(probe_status="queued"))
            await db.commit()
        self.runner = asyncio.create_task(self._run(), name="grabbit-media-probe")

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
                logger.exception("media probe worker failed")
            await asyncio.sleep(2)

    async def tick(self) -> None:
        async with SessionLocal() as db:
            # Do not contend with a video download or merge for the heavy-process slot.
            busy = await db.scalar(select(Task.id).where((Task.status.in_(("resolving", "downloading", "postprocessing"))) | (Task.phase == "subtitle_retry")).limit(1))
            analyzing = await db.scalar(select(VideoAnalysisJob.id).where(VideoAnalysisJob.status == "running").limit(1))
            if busy is not None or analyzing is not None:
                return
            record = await db.scalar(select(FileRecord).where(FileRecord.probe_status == "queued").order_by(FileRecord.created_at, FileRecord.id).limit(1))
            if record is None:
                return
            record.probe_status = "running"
            await db.commit()
            file_id, relative = record.id, record.relative_path
        try:
            path = safe_subdirectory(self.settings.download_root, relative)
            if not path.is_file() or path.is_symlink():
                media, mtime, size, status = None, None, None, "failed"
            else:
                before = path.stat()
                media = await inspect_media(path)
                after = path.stat()
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    media, mtime, size, status = None, None, None, "unprobed"
                else:
                    mtime = after.st_mtime_ns
                    size = after.st_size
                    status = "completed" if media else "failed"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("media probe failed for %s", file_id)
            media, mtime, size, status = None, None, None, "failed"
        async with SessionLocal() as db:
            record = await db.get(FileRecord, file_id)
            if record is not None and record.probe_status == "running" and record.relative_path == relative:
                unchanged = record.mtime_ns == mtime and record.size_bytes == size
                record.probe_status = status if unchanged or status != "completed" else "unprobed"
                record.media_json = json.dumps(media) if media and unchanged else None
                record.media_mtime_ns = mtime if unchanged else None
                record.updated_at = utcnow()
                await db.commit()
