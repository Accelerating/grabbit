"""Persisted, strictly bounded Debian package installation workflow."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path

from sqlalchemy import select, update

from app.db.models import DependencyInstallJob, FileRecord, RuntimeControl, Setting, VideoAnalysisJob, utcnow
from app.db.session import SessionLocal

logger = logging.getLogger("grabbit.dependency_install")
HELPER = Path("/usr/local/libexec/grabbit-install")
MAX_LOG_BYTES = 65536
INSTALL_TIMEOUT = 1200


def debian_13() -> bool:
    try:
        values = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
        return values.get("ID", "").strip('"') == "debian" and values.get("VERSION_ID", "").strip('"') == "13"
    except OSError:
        return False


def helper_available() -> bool:
    try:
        mode = HELPER.stat()
        return debian_13() and HELPER.is_file() and not HELPER.is_symlink() and mode.st_uid == 0 and not mode.st_mode & 0o022 and shutil.which("sudo") is not None
    except OSError:
        return False


def available_memory_bytes() -> int | None:
    candidates: list[int] = []
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) * 1024)
    except (OSError, ValueError, IndexError):
        pass
    try:
        limit = Path("/sys/fs/cgroup/memory.max").read_text().strip()
        if limit != "max":
            current = int(Path("/sys/fs/cgroup/memory.current").read_text().strip())
            candidates.append(max(0, int(limit) - current))
    except (OSError, ValueError):
        pass
    return min(candidates) if candidates else None


async def run_helper(action: str) -> tuple[bool, str, str | None]:
    if action not in {"aria2", "ffmpeg"}:
        return False, "Unsupported action", "INVALID_ACTION"
    if not helper_available():
        return False, "Debian 13 installation helper is unavailable", "HELPER_UNAVAILABLE"
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", str(HELPER), action,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
        )
    except OSError as exc:
        return False, type(exc).__name__, "START_FAILED"
    chunks: list[bytes] = []
    size = 0
    deadline = time.monotonic() + INSTALL_TIMEOUT
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            chunk = await asyncio.wait_for(proc.stdout.read(4096), remaining)
            if not chunk:
                break
            if size < MAX_LOG_BYTES:
                kept = chunk[: MAX_LOG_BYTES - size]
                chunks.append(kept)
                size += len(kept)
        await asyncio.wait_for(proc.wait(), max(0.1, deadline - time.monotonic()))
        log = b"".join(chunks).decode("utf-8", errors="replace")
        return proc.returncode == 0, log, None if proc.returncode == 0 else "INSTALL_FAILED"
    except asyncio.CancelledError:
        await _terminate_group(proc)
        raise
    except asyncio.TimeoutError:
        await _terminate_group(proc)
        return False, b"".join(chunks).decode("utf-8", errors="replace") + "\nTimed out", "TIMEOUT"


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


class DependencyInstallWorker:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.runner: asyncio.Task | None = None

    async def start(self) -> None:
        async with SessionLocal() as db:
            await db.execute(update(DependencyInstallJob).where(DependencyInstallJob.status == "running").values(
                status="failed", error_code="INTERRUPTED", finished_at=utcnow(), updated_at=utcnow()))
            control = await db.get(RuntimeControl, 1)
            if control and control.blocked_reason == "dependency_install" and not await db.scalar(select(DependencyInstallJob.id).where(DependencyInstallJob.status == "queued").limit(1)):
                control.dispatch_suspended = False
                control.blocked_reason = None
                control.revision += 1
            await db.commit()
        self.runner = asyncio.create_task(self._run(), name="grabbit-dependency-install")

    async def close(self) -> None:
        if self.runner:
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
                logger.exception("dependency installation tick failed")
            await asyncio.sleep(2)

    async def tick(self) -> None:
        async with SessionLocal() as db:
            job = await db.scalar(select(DependencyInstallJob).where(DependencyInstallJob.status == "queued").order_by(DependencyInstallJob.created_at).limit(1))
            if not job:
                return
            if self.scheduler.active or self.scheduler.video_active or self.scheduler.subtitle_active:
                return
            if await db.scalar(select(VideoAnalysisJob.id).where(VideoAnalysisJob.status == "running").limit(1)) or await db.scalar(select(FileRecord.id).where(FileRecord.probe_status == "running").limit(1)):
                return
            free = shutil.disk_usage(self.scheduler.settings.download_root).free
            configured = await db.get(Setting, "disk_min_free_bytes")
            minimum = max(536870912, int(json.loads(configured.value_json)) if configured else 1073741824)
            memory = available_memory_bytes()
            resource_low = free < minimum or memory is None or memory < 268435456
            action, job_id = job.action, job.id
            job.status = "running"
            job.updated_at = utcnow()
            await db.commit()
        if action == "aria2":
            async with self.scheduler._lock:
                self.scheduler.maintenance = True
                if self.scheduler.available:
                    try:
                        await self.scheduler.engine.close()
                    except Exception:
                        logger.exception("could not stop aria2 before installation")
                    self.scheduler.available = False
        try:
            try:
                success, log, error = (False, "Insufficient free disk or available memory", "RESOURCE_LOW") if resource_low else await run_helper(action)
            except Exception as exc:
                logger.exception("installation helper failed")
                success, log, error = False, type(exc).__name__, "HELPER_ERROR"
        finally:
            if action == "aria2":
                self.scheduler.maintenance = False
                self.scheduler._next_engine_retry = 0
        async with SessionLocal() as db:
            job = await db.get(DependencyInstallJob, job_id)
            if job:
                job.status = "completed" if success else "failed"
                job.log_text = log
                job.error_code = error
                job.updated_at = job.finished_at = utcnow()
            control = await db.get(RuntimeControl, 1)
            if control and control.blocked_reason == "dependency_install":
                control.dispatch_suspended = False
                control.blocked_reason = None
                control.revision += 1
            await db.commit()
