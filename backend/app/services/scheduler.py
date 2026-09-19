from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import secrets
import shutil
import signal
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

from app.api.tasks import bump_queue_revision
from app.api.system import read_settings
from app.core.config import Settings
from app.core.paths import safe_subdirectory
from app.db.models import Aria2Binding, FileRecord, RuntimeControl, Task, TaskAttempt, TaskSource, VideoAnalysisJob, WorkItem, utcnow
from app.services.torrent import parse_torrent, persist_torrent_blob, torrent_blob_path, MAX_UPLOAD_BYTES
from app.services.cookies import cookie_blob_path
from app.services.video import probe_media
from app.db.session import SessionLocal

logger = logging.getLogger("grabbit.scheduler")
RUNNING = {"resolving", "downloading", "postprocessing"}


def aria2_failure_summary(status: dict) -> str:
    """Return bounded, single-line engine diagnostics safe for task JSON/UI."""
    message = " ".join(str(status.get("errorMessage") or "").split())
    engine_code = " ".join(str(status.get("errorCode") or "").split())
    if message and engine_code:
        return f"aria2 error {engine_code}: {message}"[:1000]
    if message:
        return message[:1000]
    if engine_code:
        return f"aria2 error {engine_code}"[:1000]
    return "Download engine reported an error without details"


def as_int(value: object) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def parse_video_progress_line(line: bytes) -> dict | None:
    prefix = b"GRABBIT_PROGRESS "
    if not line.startswith(prefix) or len(line) > 16384:
        return None
    try:
        raw = json.loads(line[len(prefix):])
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    def number(key: str) -> int | None:
        value = raw.get(key)
        return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10**15 else None
    downloaded = number("downloaded_bytes")
    total = number("total_bytes")
    estimated = False
    if total is None:
        total = number("total_bytes_estimate")
        estimated = total is not None
    if downloaded is None:
        return None
    speed = number("speed")
    eta = number("eta")
    return {"scope": "current_stream", "percent": round(downloaded / total * 100, 1) if total else None,
            "downloaded_bytes": downloaded, "total_bytes": total, "total_is_estimate": estimated,
            "speed_bps": speed, "eta_seconds": eta}


class Scheduler:
    """The only component allowed to start download engine work."""

    def __init__(self, engine, settings: Settings, event_bus=None):
        self.engine = engine
        self.settings = settings
        self.event_bus = event_bus
        self.active: dict[str, str] = {}  # task id -> aria2 GID
        self.video_active: dict[str, tuple[asyncio.subprocess.Process, str, Path | None]] = {}
        self.subtitle_active: dict[str, tuple[asyncio.subprocess.Process, Path | None]] = {}
        self.video_progress_sample: dict[str, tuple[float, int]] = {}
        self.video_progress_readers: dict[str, asyncio.Task] = {}
        self.video_progress_latest: dict[str, dict] = {}
        self.available = False
        self.maintenance = False
        self._runner: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._next_engine_retry = 0.0

    async def _publish_task(self, task: Task) -> None:
        if self.event_bus is not None:
            await self.event_bus.publish("task.updated", {
                "task_id": task.id, "revision": task.revision,
                "status": task.status, "phase": task.phase,
                "progress": json.loads(task.progress_json or "{}"),
                "pending_action": task.pending_action,
            })

    async def _publish_queue(self) -> None:
        if self.event_bus is not None:
            async with SessionLocal() as db:
                from app.db.models import Setting
                row = await db.get(Setting, "queue_revision")
                await self.event_bus.publish("queue.updated", {"revision": int(row.value_json) if row else 0})

    async def start(self) -> None:
        # A previous service process may have died before removing its private
        # execution snapshots. The service is single-instance by design.
        for stale in self.settings.private_root.glob("video-cookie-*.txt"):
            if stale.is_file() and not stale.is_symlink():
                stale.unlink(missing_ok=True)
        try:
            await self.engine.start()
            self.available = True
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            logger.warning("aria2 unavailable: %s", type(exc).__name__)
            self.available = False
            self._next_engine_retry = time.monotonic() + 10
        await self.recover()
        self._runner = asyncio.create_task(self._run(), name="grabbit-scheduler")

    async def close(self) -> None:
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        if self.available:
            await self.engine.close()
        for process, _attempt_id, snapshot in list(self.video_active.values()):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if snapshot:
                snapshot.unlink(missing_ok=True)
        self.video_active.clear()
        for reader in self.video_progress_readers.values():
            reader.cancel()
        self.video_progress_readers.clear()
        self.video_progress_latest.clear()
        for process, snapshot in list(self.subtitle_active.values()):
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            if snapshot:
                snapshot.unlink(missing_ok=True)
        self.subtitle_active.clear()
        self.video_progress_sample.clear()

    async def recover(self) -> None:
        async with SessionLocal() as db:
            if await db.get(RuntimeControl, 1) is None:
                db.add(RuntimeControl(id=1, dispatch_suspended=False, revision=1))
            interrupted = (await db.scalars(select(Task).where(Task.status.in_(RUNNING)))).all()
            last_position = await db.scalar(select(func.max(WorkItem.position))) or 0
            for task in interrupted:
                if task.id in self.video_active:
                    continue
                if task.kind == "video" and task.pending_action == "stop":
                    task.status = "stopped"
                    task.phase = None
                    task.pending_action = None
                    task.finished_at = utcnow()
                    task.revision += 1
                    continue
                if task.pending_action in {"cancel", "pause"}:
                    binding = await db.scalar(select(Aria2Binding).where(Aria2Binding.task_id == task.id).order_by(Aria2Binding.created_at.desc()).limit(1))
                    if self.available and binding is not None:
                        try:
                            await self.engine.tell_status(binding.gid)
                        except Exception:
                            pass
                        else:
                            # Keep the intent until _poll observes the engine's
                            # terminal state. Completion may have won the race.
                            self.active[task.id] = binding.gid
                            continue
                    task.status = "cancelled" if task.pending_action == "cancel" else "paused"
                    task.finished_at = utcnow() if task.pending_action == "cancel" else None
                    task.phase = None
                    task.pending_action = None
                    task.revision += 1
                    continue
                task.status = "queued"
                task.phase = None
                task.revision += 1
                existing = await db.scalar(select(WorkItem.id).where(WorkItem.resource_id == task.id))
                if existing is None:
                    last_position += 1
                    work_type = "video" if task.kind == "video" else "metadata" if task.source_type == "magnet" and not json.loads(task.selection_json or "{}").get("selected_indices") else "download"
                    db.add(WorkItem(work_type=work_type, resource_id=task.id, position=last_position))
            if interrupted:
                await bump_queue_revision(db)
            attempts = (await db.scalars(select(TaskAttempt).where(TaskAttempt.status.in_({"starting", "running"})))).all()
            for attempt in attempts:
                if attempt.engine_ref not in self.active.values() and attempt.task_id not in self.video_active:
                    attempt.status = "interrupted"
                    attempt.finished_at = utcnow()
            await db.commit()
            for task in interrupted:
                await self._publish_task(task)

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(1)

    async def tick(self) -> None:
        async with self._lock:
            await self._ensure_engine()
            async with SessionLocal() as db:
                editable, _ = await read_settings(db)
                free = shutil.disk_usage(self.settings.download_root).free
                control = await db.get(RuntimeControl, 1)
                if control and free < editable["disk_min_free_bytes"]:
                    if not control.dispatch_suspended or control.blocked_reason != "disk_low":
                        control.dispatch_suspended = True
                        control.blocked_reason = "disk_low"
                        control.revision += 1
                    for task_id in self.active:
                        task = await db.get(Task, task_id)
                        if task and not task.pending_action:
                            task.pending_action = "pause"
                            task.blocked_reason = "disk_low"
                            task.revision += 1
                    for task_id in self.video_active:
                        task = await db.get(Task, task_id)
                        if task and not task.pending_action:
                            task.pending_action = "stop"
                            task.blocked_reason = "disk_low"
                            task.revision += 1
                    await db.commit()
            for task_id, gid in list(self.active.items()):
                await self._poll(task_id, gid)
            for task_id in list(self.video_active):
                await self._poll_video(task_id)
            for task_id in list(self.subtitle_active):
                await self._poll_subtitle_retry(task_id)
            async with SessionLocal() as db:
                editable, _ = await read_settings(db)
                max_active = editable["max_active_tasks"]
                control = await db.get(RuntimeControl, 1)
                if control and control.dispatch_suspended:
                    return
                if len(self.active) + len(self.video_active) + len(self.subtitle_active) >= max_active:
                    return
                if await db.scalar(select(VideoAnalysisJob.id).where(VideoAnalysisJob.status == "running").limit(1)) is not None or await db.scalar(select(FileRecord.id).where(FileRecord.probe_status == "running").limit(1)) is not None:
                    return
                statement = select(WorkItem).where(WorkItem.work_type.in_({"download", "metadata", "video", "subtitle_retry"}), WorkItem.available_at <= utcnow())
                if self.subtitle_active:
                    statement = statement.where(WorkItem.resource_id.not_in(self.subtitle_active))
                item = await db.scalar(statement.order_by(WorkItem.position).limit(1))
                if item is None:
                    return
                task = await db.get(Task, item.resource_id)
                if item.work_type == "subtitle_retry":
                    if task is None or task.kind != "video" or task.status != "completed":
                        await db.delete(item)
                        await bump_queue_revision(db)
                        await db.commit()
                        await self._publish_queue()
                    else:
                        await self._dispatch_subtitle_retry(db, task, item)
                    return
                if task is None or task.status not in {"queued", "retry_wait"}:
                    await db.delete(item)
                    await bump_queue_revision(db)
                    await db.commit()
                    await self._publish_queue()
                    return
                if task.status == "retry_wait":
                    task.status = "queued"
                    task.next_retry_at = None
                if item.work_type == "video":
                    await self._dispatch_video(db, task, item)
                elif self.available:
                    await self._dispatch(db, task, item)

    async def _ensure_engine(self) -> None:
        if self.maintenance:
            return
        running = getattr(self.engine, "is_running", True)
        if self.available and running:
            return
        if time.monotonic() < self._next_engine_retry:
            return
        self.available = False
        self.active.clear()
        self._next_engine_retry = time.monotonic() + 10
        try:
            await self.engine.start()
        except (FileNotFoundError, OSError, RuntimeError) as exc:
            logger.warning("aria2 restart unavailable: %s", type(exc).__name__)
            return
        self.available = True
        await self.recover()

    async def _dispatch(self, db, task: Task, item: WorkItem) -> None:
        metadata = item.work_type == "metadata"
        role = "metadata" if metadata else "content"
        previous = await db.scalar(select(Aria2Binding).where(Aria2Binding.task_id == task.id, Aria2Binding.role == role).order_by(Aria2Binding.created_at.desc()).limit(1))
        if previous and previous.last_engine_state in {"starting", "active", "paused", "interrupted"}:
            try:
                observed = await self.engine.tell_status(previous.gid)
            except Exception:
                previous.last_engine_state = "missing"
            else:
                state = observed.get("status")
                if state == "complete":
                    await db.delete(item)
                    await bump_queue_revision(db)
                    if metadata:
                        await self._metadata_complete(db, task, previous.gid, observed)
                    else:
                        await self._complete(db, task, previous.gid)
                    return
                if state in {"active", "waiting", "paused"}:
                    if state == "paused":
                        await self.engine.unpause(previous.gid)
                    task.status = "resolving" if metadata else "downloading"
                    task.phase = "metadata" if metadata else "download"
                    task.revision += 1
                    previous.last_engine_state = "active"
                    prior_attempt = await db.get(TaskAttempt, previous.attempt_id)
                    if prior_attempt:
                        prior_attempt.status = "running"
                        prior_attempt.finished_at = None
                    await db.delete(item)
                    await bump_queue_revision(db)
                    await db.commit()
                    await self._publish_task(task)
                    await self._publish_queue()
                    self.active[task.id] = previous.gid
                    return
        task_dir = safe_subdirectory(self.settings.download_root, task.task_directory_key)
        task_dir.mkdir(parents=True, exist_ok=True)
        gid = secrets.token_hex(8)
        attempt = TaskAttempt(task_id=task.id, cycle=task.retry_cycle, ordinal=task.attempt_count + 1, engine="aria2", status="starting", engine_ref=gid)
        db.add(attempt)
        await db.flush()
        db.add(Aria2Binding(task_id=task.id, attempt_id=attempt.id, gid=gid, role=role, last_engine_state="starting"))
        task.status = "resolving" if metadata else "downloading"
        task.phase = "metadata" if metadata else "download"
        task.started_at = task.started_at or utcnow()
        task.attempt_count += 1
        task.revision += 1
        await db.delete(item)
        await bump_queue_revision(db)
        await db.commit()  # stable GID is durable before the external side effect
        await self._publish_task(task)
        await self._publish_queue()
        try:
            if metadata:
                await self.engine.add_magnet_metadata(task.source_url, task_dir, gid)
            elif task.source_type in {"torrent", "magnet"}:
                source = await db.get(TaskSource, task.id)
                if source is None or not source.torrent_blob_key:
                    raise FileNotFoundError("Torrent source is missing")
                blob_path = torrent_blob_path(self.settings.private_root, source.torrent_blob_key)
                blob = await asyncio.to_thread(blob_path.read_bytes)
                indices = json.loads(task.selection_json)["selected_indices"]
                await self.engine.add_torrent(blob, task_dir, gid, indices)
            else:
                await self.engine.add_uri(task.source_url, task_dir, gid)
            await self.engine.unpause(gid)
            async with SessionLocal() as update_db:
                current_attempt = await update_db.get(TaskAttempt, attempt.id)
                current_binding = await update_db.scalar(select(Aria2Binding).where(Aria2Binding.gid == gid))
                if current_attempt:
                    current_attempt.status = "running"
                if current_binding:
                    current_binding.last_engine_state = "active"
                await update_db.commit()
            self.active[task.id] = gid
        except Exception as exc:
            detail = " ".join(str(exc).split())[:1000]
            logger.warning("aria2 start failed for task %s: %s: %s", task.id, type(exc).__name__, detail)
            await self._fail(task.id, gid, "PROCESS_FAILED", detail or type(exc).__name__)

    async def _dispatch_video(self, db, task: Task, item: WorkItem) -> None:
        executable = shutil.which("yt-dlp")
        if executable is None:
            task.status = "failed"
            task.error_code = "DEPENDENCY_MISSING"
            task.error_summary = "yt-dlp is not installed"
            task.finished_at = utcnow()
            task.revision += 1
            await db.delete(item)
            await bump_queue_revision(db)
            await db.commit()
            await self._publish_task(task)
            await self._publish_queue()
            return
        task_dir = safe_subdirectory(self.settings.download_root, task.task_directory_key)
        task_dir.mkdir(parents=True, exist_ok=True)
        snapshot = None
        cookie_version = None
        if task.cookie_profile_id:
            from app.db.models import CookieProfile
            profile = await db.get(CookieProfile, task.cookie_profile_id)
            if profile is None:
                task.status = "failed"
                task.error_code = "COOKIE_MISSING"
                task.error_summary = "Cookie profile is missing"
                task.finished_at = utcnow()
                task.revision += 1
                await db.delete(item)
                await bump_queue_revision(db)
                await db.commit()
                await self._publish_task(task)
                await self._publish_queue()
                return
            try:
                source = cookie_blob_path(self.settings.private_root, profile.secret_file_key)
                if not source.is_file() or source.is_symlink():
                    raise FileNotFoundError
                descriptor, name = tempfile.mkstemp(prefix="video-cookie-", suffix=".txt", dir=self.settings.private_root)
                snapshot = Path(name)
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
                os.chmod(snapshot, 0o600)
                cookie_version = profile.content_version
                profile.last_used_at = utcnow()
            except OSError:
                if snapshot:
                    snapshot.unlink(missing_ok=True)
                task.status = "failed"
                task.error_code = "COOKIE_UNAVAILABLE"
                task.error_summary = "Cookie content is unavailable"
                task.finished_at = utcnow()
                task.revision += 1
                await db.delete(item)
                await bump_queue_revision(db)
                await db.commit()
                await self._publish_task(task)
                await self._publish_queue()
                return
        options = json.loads(task.options_json or "{}")
        mode = options.get("mode", "video")
        height = as_int(options.get("max_height", 1080)) or 1080
        format_selector = "bestaudio" if mode == "audio" else f"bv*[height<={height}]+ba/b[height<={height}]"
        args = [executable, "--ignore-config", "--no-playlist", "--no-warnings", "--newline", "--progress-template", "download:GRABBIT_PROGRESS %(progress)j", "--progress-delta", "1", "--socket-timeout", "30", "--retries", "3", "--fragment-retries", "3", "--format", format_selector, "--paths", str(task_dir), "--output", "%(title).200B [%(id)s].%(ext)s"]
        if snapshot:
            args.extend(["--cookies", str(snapshot)])
        args.extend(["--", task.source_url])
        attempt = TaskAttempt(task_id=task.id, cycle=task.retry_cycle, ordinal=task.attempt_count + 1, engine="yt-dlp", status="starting", cookie_version=cookie_version)
        db.add(attempt)
        task.status = "downloading"
        task.phase = "video_download"
        task.started_at = task.started_at or utcnow()
        task.attempt_count += 1
        task.revision += 1
        await db.delete(item)
        await bump_queue_revision(db)
        await db.commit()
        await self._publish_task(task)
        await self._publish_queue()
        try:
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        except OSError:
            if snapshot:
                snapshot.unlink(missing_ok=True)
            await self._video_failed(task.id, attempt.id, "PROCESS_FAILED")
            return
        async with SessionLocal() as update_db:
            current = await update_db.get(TaskAttempt, attempt.id)
            if current:
                current.engine_ref = f"pid:{process.pid}"
                current.status = "running"
                await update_db.commit()
        self.video_active[task.id] = (process, attempt.id, snapshot)
        self.video_progress_readers[task.id] = asyncio.create_task(self._read_video_progress(task.id, process.stdout), name=f"grabbit-video-progress-{task.id}")

    async def _read_video_progress(self, task_id: str, stream: asyncio.StreamReader) -> None:
        try:
            while line := await stream.readline():
                progress = parse_video_progress_line(line.strip())
                if progress is not None:
                    self.video_progress_latest[task_id] = progress
        except (ValueError, asyncio.LimitOverrunError):
            logger.warning("video progress output exceeded line limit for task %s", task_id)

    async def _poll_video(self, task_id: str) -> None:
        process, attempt_id, snapshot = self.video_active[task_id]
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            if task.pending_action == "stop" and process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if process.returncode is None:
                if task.phase == "video_download" and not task.pending_action:
                    folder = safe_subdirectory(self.settings.download_root, task.task_directory_key)
                    downloaded = 0
                    for path in folder.rglob("*"):
                        try:
                            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(folder.resolve()):
                                downloaded += path.stat().st_size
                        except OSError:
                            continue
                    now = time.monotonic()
                    previous = self.video_progress_sample.get(task_id)
                    speed = max(0, int((downloaded - previous[1]) / (now - previous[0]))) if previous and now > previous[0] else 0
                    self.video_progress_sample[task_id] = (now, downloaded)
                    progress = self.video_progress_latest.get(task_id) or {"scope": "current_stream", "downloaded_bytes": downloaded, "total_bytes": None, "percent": None, "speed_bps": speed, "eta_seconds": None}
                    if progress != json.loads(task.progress_json or "{}"):
                        task.progress_json = json.dumps(progress)
                        task.revision += 1
                        task.updated_at = utcnow()
                        await db.commit()
                        await self._publish_task(task)
                return
            await process.wait()
            reader = self.video_progress_readers.pop(task_id, None)
            if reader is not None:
                await reader
            self.video_progress_latest.pop(task_id, None)
            self.video_progress_sample.pop(task_id, None)
            attempt = await db.get(TaskAttempt, attempt_id)
            subtitle_options = json.loads(task.options_json or "{}")
            subtitle_languages = subtitle_options.get("subtitle_languages", ["zh.*", "en.*"])
            if task.phase == "video_download" and not task.pending_action and process.returncode == 0 and subtitle_options.get("mode", "video") == "video" and subtitle_languages:
                executable = shutil.which("yt-dlp")
                if executable:
                    folder = safe_subdirectory(self.settings.download_root, task.task_directory_key)
                    args = [executable, "--ignore-config", "--no-playlist", "--no-warnings", "--socket-timeout", "30", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", ",".join(subtitle_languages), "--sub-format", "vtt/srt/ass/best", "--convert-subs", "vtt", "--paths", str(folder), "--output", "%(title).200B [%(id)s].%(ext)s"]
                    if snapshot:
                        args.extend(["--cookies", str(snapshot)])
                    args.extend(["--", task.source_url])
                    try:
                        subtitle_process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
                    except OSError:
                        task.warnings_json = json.dumps(["SUBTITLE_FAILED"])
                    else:
                        task.phase = "video_subtitles"
                        task.progress_json = json.dumps({"scope": "subtitles", "percent": None})
                        task.revision += 1
                        self.video_active[task_id] = (subtitle_process, attempt_id, snapshot)
                        await db.commit()
                        await self._publish_task(task)
                        return
            if snapshot:
                snapshot.unlink(missing_ok=True)
            self.video_active.pop(task_id, None)
            if task.pending_action == "stop":
                task.status = "stopped"
                task.pending_action = None
                task.phase = None
                task.finished_at = utcnow()
                task.revision += 1
                if attempt:
                    attempt.status = "stopped"
                    attempt.finished_at = utcnow()
                await db.commit()
                await self._publish_task(task)
                return
            if process.returncode != 0 and task.phase != "video_subtitles":
                await db.commit()
                await self._video_failed(task_id, attempt_id, "PROCESS_FAILED")
                return
            if process.returncode != 0 and task.phase == "video_subtitles":
                task.warnings_json = json.dumps(["SUBTITLE_FAILED"])
            root = self.settings.download_root.resolve()
            folder = safe_subdirectory(root, task.task_directory_key).resolve()
            media_count = 0
            indexed_paths = []
            for path in folder.rglob("*"):
                if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(folder):
                    continue
                if path.suffix.lower() in {".part", ".aria2", ".ytdl"}:
                    continue
                mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                if mime.startswith(("video/", "audio/")):
                    if not await probe_media(path):
                        continue
                    media_count += 1
                indexed_paths.append((path, mime))
            if media_count == 0:
                await db.commit()
                await self._video_failed(task_id, attempt_id, "MEDIA_INVALID")
                return
            for path, mime in indexed_paths:
                stat = path.stat()
                relative = path.resolve().relative_to(root).as_posix()
                existing = await db.scalar(select(FileRecord).where(FileRecord.relative_path == relative))
                if existing is None:
                    db.add(FileRecord(task_id=task.id, relative_path=relative, display_name=path.name, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, mime_type=mime, kind="video" if mime.startswith("video/") else "audio" if mime.startswith("audio/") else "other", availability="present", is_complete=True))
            task.status = "completed"
            task.phase = None
            task.finished_at = utcnow()
            task.progress_json = json.dumps({"scope": "task", "percent": 100})
            task.revision += 1
            if attempt:
                attempt.status = "completed"
                attempt.finished_at = utcnow()
            await db.commit()
            await self._publish_task(task)

    async def _video_failed(self, task_id: str, attempt_id: str, code: str) -> None:
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            task.status = "failed"
            task.phase = None
            task.pending_action = None
            task.error_code = code
            task.error_summary = "Video download failed"
            task.finished_at = utcnow()
            task.revision += 1
            attempt = await db.get(TaskAttempt, attempt_id)
            if attempt:
                attempt.status = "failed"
                attempt.error_code = code
                attempt.finished_at = utcnow()
            await db.commit()
            await self._publish_task(task)

    async def _poll(self, task_id: str, gid: str) -> None:
        try:
            status = await self.engine.tell_status(gid)
        except Exception as exc:
            logger.warning("aria2 status failed for task %s: %s", task_id, type(exc).__name__)
            return
        state = status.get("status")
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                self.active.pop(task_id, None)
                return
            binding = await db.scalar(select(Aria2Binding).where(Aria2Binding.gid == gid))
            metadata = binding is not None and binding.role == "metadata"
            if state == "complete":
                if metadata:
                    await self._metadata_complete(db, task, gid, status)
                else:
                    await self._complete(db, task, gid)
                return
            if task.pending_action:
                action = task.pending_action
                try:
                    if action == "pause":
                        if state != "paused":
                            await self.engine.pause(gid)
                    elif action == "cancel":
                        if state != "removed":
                            await self.engine.remove(gid)
                except Exception as exc:
                    logger.warning("aria2 %s failed for task %s: %s", action, task_id, type(exc).__name__)
                    return
                try:
                    confirmed = await self.engine.tell_status(gid)
                except Exception:
                    return
                if confirmed.get("status") == "complete":
                    if metadata:
                        await self._metadata_complete(db, task, gid, confirmed)
                    else:
                        await self._complete(db, task, gid)
                    return
                if confirmed.get("status") != ("paused" if action == "pause" else "removed"):
                    return
                task.status = "paused" if action == "pause" else "cancelled"
                task.pending_action = None
                task.phase = None
                task.finished_at = utcnow() if action == "cancel" else None
                task.revision += 1
                binding = await db.scalar(select(Aria2Binding).where(Aria2Binding.gid == gid))
                if binding:
                    binding.last_engine_state = "paused" if action == "pause" else "removed"
                await db.commit()
                await self._publish_task(task)
                self.active.pop(task_id, None)
                return
            if state == "error":
                summary = aria2_failure_summary(status)
                logger.warning("aria2 task %s failed: %s", task_id, summary)
                await db.commit()
                await self._fail(task_id, gid, "NETWORK_ERROR", summary)
                return
            if metadata:
                attempt = await db.scalar(select(TaskAttempt).where(TaskAttempt.engine_ref == gid))
                if attempt and (utcnow() - attempt.started_at.replace(tzinfo=timezone.utc)).total_seconds() > 180:
                    try:
                        await self.engine.remove(gid)
                    except Exception:
                        pass
                    await db.commit()
                    await self._fail(task_id, gid, "METADATA_TIMEOUT")
                    return
            downloaded = as_int(status.get("completedLength"))
            total = as_int(status.get("totalLength"))
            speed = as_int(status.get("downloadSpeed"))
            progress = {
                "scope": "task", "downloaded_bytes": downloaded,
                "total_bytes": total or None, "percent": round(downloaded / total * 100, 1) if total else None,
                "speed_bps": speed, "eta_seconds": (total - downloaded) // speed if total and speed else None,
            }
            if progress != json.loads(task.progress_json or "{}"):
                task.progress_json = json.dumps(progress)
                task.revision += 1
                task.updated_at = utcnow()
                await db.commit()
                await self._publish_task(task)

    async def _metadata_complete(self, db, task: Task, gid: str, status: dict) -> None:
        source = await db.get(TaskSource, task.id)
        if source is None or not source.magnet_infohash:
            await db.commit()
            await self._fail(task.id, gid, "METADATA_INVALID")
            return
        task_dir = safe_subdirectory(self.settings.download_root, task.task_directory_key)
        candidate = task_dir / f"{source.magnet_infohash}.torrent"
        if not candidate.exists():
            candidate = task_dir / f"{source.magnet_infohash.upper()}.torrent"
        try:
            if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > MAX_UPLOAD_BYTES:
                raise ValueError("Metadata file is missing or too large")
            blob = await asyncio.to_thread(candidate.read_bytes)
            parsed = parse_torrent(blob)
            if parsed.infohash != source.magnet_infohash:
                raise ValueError("Metadata infohash mismatch")
            blob_key = persist_torrent_blob(self.settings.private_root, blob, task.id)
        except (OSError, ValueError):
            await db.commit()
            await self._fail(task.id, gid, "METADATA_INVALID")
            return
        source.torrent_blob_key = blob_key
        source.source_snapshot_json = json.dumps(parsed.summary)
        task.title = parsed.summary["name"]
        task.status = "awaiting_selection"
        task.phase = None
        task.pending_action = None
        task.progress_json = "{}"
        task.revision += 1
        task.updated_at = utcnow()
        attempt = await db.scalar(select(TaskAttempt).where(TaskAttempt.engine_ref == gid))
        if attempt:
            attempt.status = "completed"
            attempt.finished_at = utcnow()
        binding = await db.scalar(select(Aria2Binding).where(Aria2Binding.gid == gid))
        if binding:
            binding.last_engine_state = "complete"
        await db.commit()
        await self._publish_task(task)
        self.active.pop(task.id, None)
        # Metadata-only mode must not start content.  Remove any paused
        # follow-up GIDs defensively before yielding the global slot.
        for followed_gid in status.get("followedBy", []) or []:
            try:
                await self.engine.remove(followed_gid)
            except Exception:
                logger.warning("failed to remove magnet follow-up for task %s", task.id)
        try:
            candidate.unlink()
        except OSError:
            pass

    async def _complete(self, db, task: Task, gid: str) -> None:
        try:
            files = await self.engine.get_files(gid)
        except Exception:
            await db.commit()
            await self._fail(task.id, gid, "PROCESS_FAILED")
            return
        root = self.settings.download_root.resolve()
        task_dir = safe_subdirectory(root, task.task_directory_key).resolve()
        indexed = 0
        for item in files:
            path = Path(item.get("path", ""))
            if item.get("selected") == "false" or not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(task_dir):
                continue
            if path.suffix in {".part", ".aria2"}:
                continue
            stat = path.stat()
            relative = resolved.relative_to(root).as_posix()
            existing = await db.scalar(select(FileRecord).where(FileRecord.relative_path == relative))
            if existing is None:
                db.add(FileRecord(task_id=task.id, relative_path=relative, display_name=path.name, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, kind="other", availability="present", is_complete=True))
            indexed += 1
        if not indexed:
            await db.commit()
            await self._fail(task.id, gid, "FILE_MISSING")
            return
        task.status = "completed"
        task.phase = None
        task.pending_action = None
        task.finished_at = utcnow()
        task.progress_json = json.dumps({"scope": "task", "percent": 100})
        task.revision += 1
        attempt = await db.scalar(select(TaskAttempt).where(TaskAttempt.engine_ref == gid))
        if attempt:
            attempt.status = "completed"
            attempt.finished_at = utcnow()
        await db.commit()
        await self._publish_task(task)
        if task.source_type == "magnet":
            source = await db.get(TaskSource, task.id)
            if source and source.magnet_infohash:
                metadata = task_dir / f"{source.magnet_infohash}.torrent"
                if metadata.is_file() and not metadata.is_symlink():
                    try:
                        metadata.unlink()
                    except OSError:
                        logger.warning("could not remove magnet metadata copy for task %s", task.id)
        self.active.pop(task.id, None)

    async def _dispatch_subtitle_retry(self, db, task: Task, item: WorkItem) -> None:
        executable = shutil.which("yt-dlp")
        options = json.loads(task.options_json or "{}")
        languages = options.get("subtitle_languages", ["zh.*", "en.*"])
        snapshot = None
        try:
            if not executable or not languages:
                raise FileNotFoundError("yt-dlp or subtitle selection is unavailable")
            folder = safe_subdirectory(self.settings.download_root, task.task_directory_key)
            if not folder.is_dir() or folder.is_symlink():
                raise FileNotFoundError("task folder is unavailable")
            if task.cookie_profile_id:
                from app.db.models import CookieProfile
                profile = await db.get(CookieProfile, task.cookie_profile_id)
                if profile is None:
                    raise FileNotFoundError("Cookie profile is unavailable")
                source = cookie_blob_path(self.settings.private_root, profile.secret_file_key)
                if not source.is_file() or source.is_symlink():
                    raise FileNotFoundError("Cookie content is unavailable")
                descriptor, name = tempfile.mkstemp(prefix="video-cookie-", suffix=".txt", dir=self.settings.private_root)
                snapshot = Path(name)
                with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output)
                os.chmod(snapshot, 0o600)
            args = [executable, "--ignore-config", "--no-playlist", "--no-warnings", "--socket-timeout", "30", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", ",".join(languages), "--sub-format", "vtt/srt/ass/best", "--convert-subs", "vtt", "--paths", str(folder), "--output", "%(title).200B [%(id)s].%(ext)s"]
            if snapshot:
                args.extend(["--cookies", str(snapshot)])
            args.extend(["--", task.source_url])
            process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        except (OSError, ValueError):
            if snapshot:
                snapshot.unlink(missing_ok=True)
            task.warnings_json = json.dumps(sorted(set(json.loads(task.warnings_json or "[]")) | {"SUBTITLE_FAILED"}))
            task.phase = None
            task.revision += 1
            await db.delete(item)
            await bump_queue_revision(db)
            await db.commit()
            await self._publish_task(task)
            await self._publish_queue()
            return
        task.phase = "subtitle_retry"
        task.revision += 1
        await db.commit()
        self.subtitle_active[task.id] = (process, snapshot)
        await self._publish_task(task)

    async def _poll_subtitle_retry(self, task_id: str) -> None:
        process, snapshot = self.subtitle_active[task_id]
        if process.returncode is None:
            return
        await process.wait()
        if snapshot:
            snapshot.unlink(missing_ok=True)
        self.subtitle_active.pop(task_id, None)
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            item = await db.scalar(select(WorkItem).where(WorkItem.work_type == "subtitle_retry", WorkItem.resource_id == task_id))
            if task is None or item is None:
                return
            if process.returncode == 0:
                root = self.settings.download_root.resolve()
                folder = safe_subdirectory(root, task.task_directory_key)
                for path in folder.rglob("*.vtt"):
                    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
                        continue
                    stat = path.stat()
                    relative = path.relative_to(root).as_posix()
                    record = await db.scalar(select(FileRecord).where(FileRecord.relative_path == relative))
                    if record is None:
                        db.add(FileRecord(task_id=task.id, relative_path=relative, display_name=path.name, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, mime_type="text/vtt", kind="other", availability="present", is_complete=True))
                    else:
                        record.size_bytes, record.mtime_ns, record.availability = stat.st_size, stat.st_mtime_ns, "present"
                task.warnings_json = json.dumps([warning for warning in json.loads(task.warnings_json or "[]") if warning != "SUBTITLE_FAILED"])
            else:
                task.warnings_json = json.dumps(sorted(set(json.loads(task.warnings_json or "[]")) | {"SUBTITLE_FAILED"}))
            task.phase = None
            task.revision += 1
            await db.delete(item)
            await bump_queue_revision(db)
            await db.commit()
            await self._publish_task(task)
            await self._publish_queue()

    async def _fail(self, task_id: str, gid: str, code: str, summary: str | None = None) -> None:
        async with SessionLocal() as db:
            task = await db.get(Task, task_id)
            if task is None:
                return
            task.error_code = code
            task.error_summary = summary or "Download attempt failed"
            task.pending_action = None
            task.phase = None
            task.revision += 1
            attempt = await db.scalar(select(TaskAttempt).where(TaskAttempt.engine_ref == gid))
            if attempt:
                attempt.status = "failed"
                attempt.error_code = code
                attempt.finished_at = utcnow()
            if task.attempt_count < 3 and code in {"NETWORK_ERROR", "PROCESS_FAILED"}:
                delay = 30 if task.attempt_count == 1 else 120
                task.status = "retry_wait"
                task.next_retry_at = utcnow() + timedelta(seconds=delay)
                max_position = await db.scalar(select(func.max(WorkItem.position))) or 0
                work_type = "metadata" if task.source_type == "magnet" and not json.loads(task.selection_json or "{}").get("selected_indices") else "download"
                db.add(WorkItem(work_type=work_type, resource_id=task.id, position=max_position + 1, available_at=task.next_retry_at))
                await bump_queue_revision(db)
            else:
                task.status = "failed"
                task.finished_at = utcnow()
            await db.commit()
            await self._publish_task(task)
            if task.status == "retry_wait":
                await self._publish_queue()
        self.active.pop(task_id, None)
