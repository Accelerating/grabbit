"""Restart-safe, single-worker file deletion operations."""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import stat
from contextlib import ExitStack
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import Settings
from app.db.models import FileOperation, FileRecord, Task, utcnow
from app.db.session import SessionLocal

logger = logging.getLogger("grabbit.file_operations")
BUSY_TASK_STATES = {"queued", "resolving", "awaiting_selection", "downloading", "postprocessing", "retry_wait", "paused"}


def _unlink_if_unchanged(root: Path, snapshot: dict) -> str:
    """Walk with directory descriptors so a replaced parent symlink cannot escape root."""
    relative = snapshot["relative_path"]
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return "changed"
    try:
        with ExitStack() as stack:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            try:
                current = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return "deleted"  # Reconcile an unlink completed before the previous commit.
            if not stat.S_ISREG(current.st_mode):
                return "changed"
            if (current.st_size, current.st_mtime_ns, current.st_dev, current.st_ino) != (
                snapshot["size_bytes"], snapshot["mtime_ns"], snapshot["dev"], snapshot["ino"]
            ):
                return "changed"
            os.unlink(parts[-1], dir_fd=fd)
            return "deleted"
    except FileNotFoundError:
        return "changed"  # Missing parent is not proof the original file was deleted.
    except OSError:
        logger.exception("could not delete file %s", relative)
        return "failed"


def _rmdir_if_unchanged(root: Path, snapshot: dict) -> str:
    parts = snapshot["relative_path"].split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return "changed"
    try:
        with ExitStack() as stack:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            try:
                current = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return "deleted"  # The prior attempt may have removed it before commit.
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (snapshot["dev"], snapshot["ino"]):
                return "changed"
            try:
                os.rmdir(parts[-1], dir_fd=fd)
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    return "not_empty"
                raise
            return "deleted"
    except FileNotFoundError:
        return "changed"
    except OSError:
        logger.exception("could not delete directory %s", snapshot["relative_path"])
        return "failed"


class FileOperationWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.runner: asyncio.Task | None = None

    async def start(self) -> None:
        async with SessionLocal() as db:
            await db.execute(update(FileOperation).where(FileOperation.status == "running").values(status="queued", updated_at=utcnow()))
            await db.commit()
        self.runner = asyncio.create_task(self._run(), name="grabbit-file-operations")

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
                logger.exception("file operation worker failed")
            await asyncio.sleep(1)

    async def tick(self) -> None:
        async with SessionLocal() as db:
            operation = await db.scalar(select(FileOperation).where(FileOperation.status == "queued").order_by(FileOperation.created_at, FileOperation.id).limit(1))
            if operation is None:
                return
            operation.status = "running"
            operation.updated_at = utcnow()
            await db.commit()
            operation_id = operation.id
        async with SessionLocal() as db:
            operation = await db.get(FileOperation, operation_id)
            if operation is None:
                return
            targets = json.loads(operation.target_snapshot_json)
            results = json.loads(operation.results_json)
            processed = {result["id"] for result in results}
            directory_root = targets[-1]["relative_path"] if operation.kind == "delete_directory" else None
            for item in targets:
                if item["id"] in processed:
                    continue
                record = await db.scalar(select(FileRecord).where(FileRecord.relative_path == item["relative_path"])) if directory_root else await db.get(FileRecord, item["id"])
                if directory_root:
                    busy = (await db.scalars(select(Task).where(Task.status.in_(BUSY_TASK_STATES)))).all()
                    in_use = any(task.task_directory_key == directory_root or task.task_directory_key.startswith(directory_root + "/") or directory_root.startswith(task.task_directory_key + "/") for task in busy)
                    if record is not None and record.task_id:
                        task = await db.get(Task, record.task_id)
                        in_use = in_use or task is None or task.status in BUSY_TASK_STATES
                    status = "in_use" if in_use else _rmdir_if_unchanged(self.settings.download_root, item) if item["type"] == "directory" else _unlink_if_unchanged(self.settings.download_root, item)
                elif record is None or record.relative_path != item["relative_path"]:
                    status = "changed"
                elif record.task_id:
                    task = await db.get(Task, record.task_id)
                    status = "in_use" if task is None or task.status in BUSY_TASK_STATES else _unlink_if_unchanged(self.settings.download_root, item)
                else:
                    status = _unlink_if_unchanged(self.settings.download_root, item)
                if status == "deleted" and record is not None:
                    record.availability = "missing"
                    record.updated_at = utcnow()
                results.append({"id": item["id"], "status": status})
                operation.results_json = json.dumps(results)
                operation.updated_at = utcnow()
                await db.commit()
            operation.status = "completed" if all(result["status"] == "deleted" for result in results) else "partial"
            operation.finished_at = utcnow()
            operation.updated_at = utcnow()
            await db.commit()
