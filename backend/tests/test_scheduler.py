from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Aria2Binding, Setting, Task, TaskAttempt, WorkItem
from app.services import scheduler as scheduler_module


class FakeAria:
    def __init__(self):
        self.added = []
        self.files = []
        self.status = "complete"

    async def start(self):
        return {"version": "test"}

    async def close(self):
        pass

    async def add_uri(self, uri, directory, gid):
        self.added.append((uri, directory, gid))
        return gid

    async def unpause(self, gid):
        return gid

    async def tell_status(self, gid):
        return {"status": self.status, "completedLength": "5", "totalLength": "5"}

    async def get_files(self, gid):
        return self.files

    async def pause(self, gid):
        self.status = "paused"
        return gid

    async def remove(self, gid):
        self.status = "removed"
        return gid


@pytest.mark.asyncio
async def test_scheduler_dispatches_one_and_indexes_real_file(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "downloads"
    root.mkdir()
    settings = Settings(download_root=root, private_root=tmp_path / "private")
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="task-one", kind="general", source_type="http", source_url="https://example.test/file", source_fingerprint="f1", title="file", status="queued", download_subdir="general", task_directory_key="general/task-one", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(Task(id="task-two", kind="general", source_type="http", source_url="https://example.test/other", source_fingerprint="f2", title="other", status="queued", download_subdir="general", task_directory_key="general/task-two", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(WorkItem(work_type="download", resource_id="task-one", position=1))
        db.add(WorkItem(work_type="download", resource_id="task-two", position=2))
        await db.commit()
    aria = FakeAria()
    scheduler = scheduler_module.Scheduler(aria, settings)
    scheduler.available = True
    await scheduler.tick()
    assert len(aria.added) == 1
    path = root / "general" / "task-one" / "file"
    path.write_bytes(b"hello")
    aria.files = [{"path": str(path), "completedLength": "5", "selected": "true"}]
    await scheduler.tick()
    async with sessions() as db:
        task = await db.get(Task, "task-one")
        assert task.status == "completed"
        assert (await db.scalar(select(scheduler_module.FileRecord.relative_path))) == "general/task-one/file"
    await engine.dispose()


@pytest.mark.asyncio
async def test_pause_releases_slot_only_after_engine_confirmation(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'pause.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "downloads"
    root.mkdir()
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="pause-task", kind="general", source_type="http", source_url="https://example.test/file", source_fingerprint="pause", title="file", status="queued", download_subdir="general", task_directory_key="general/pause-task", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(WorkItem(work_type="download", resource_id="pause-task", position=1))
        await db.commit()
    aria = FakeAria()
    aria.status = "active"
    scheduler = scheduler_module.Scheduler(aria, Settings(download_root=root, private_root=tmp_path / "private"))
    scheduler.available = True
    await scheduler.tick()
    async with sessions() as db:
        task = await db.get(Task, "pause-task")
        task.pending_action = "pause"
        await db.commit()
    assert "pause-task" in scheduler.active
    await scheduler.tick()
    assert "pause-task" not in scheduler.active
    async with sessions() as db:
        assert (await db.get(Task, "pause-task")).status == "paused"
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_reuses_existing_gid_instead_of_duplicate_add(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recover.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "downloads"
    task_dir = root / "general" / "recover-task"
    task_dir.mkdir(parents=True)
    file = task_dir / "done.bin"
    file.write_bytes(b"done")
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="recover-task", kind="general", source_type="http", source_url="https://example.test/done.bin", source_fingerprint="recover", title="done", status="downloading", download_subdir="general", task_directory_key="general/recover-task", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(TaskAttempt(id="recover-attempt", task_id="recover-task", cycle=0, ordinal=1, engine="aria2", status="running", engine_ref="1234567890abcdef"))
        db.add(Aria2Binding(task_id="recover-task", attempt_id="recover-attempt", gid="1234567890abcdef", role="content", last_engine_state="active"))
        await db.commit()
    aria = FakeAria()
    aria.files = [{"path": str(file), "completedLength": "4", "selected": "true"}]
    scheduler = scheduler_module.Scheduler(aria, Settings(download_root=root, private_root=tmp_path / "private"))
    scheduler.available = True
    await scheduler.recover()
    await scheduler.tick()
    assert aria.added == []
    async with sessions() as db:
        assert (await db.get(Task, "recover-task")).status == "completed"
    await engine.dispose()


@pytest.mark.asyncio
async def test_recovery_pending_cancel_does_not_hide_completed_download(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-race.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "downloads"
    task_dir = root / "general" / "race-task"
    task_dir.mkdir(parents=True)
    output = task_dir / "done.bin"
    output.write_bytes(b"done")
    gid = "1234567890abcdef"
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="race-task", kind="general", source_type="http", source_url="https://example.test/done.bin", source_fingerprint="race", title="done", status="downloading", pending_action="cancel", download_subdir="general", task_directory_key="general/race-task", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(TaskAttempt(id="race-attempt", task_id="race-task", cycle=0, ordinal=1, engine="aria2", status="running", engine_ref=gid))
        db.add(Aria2Binding(task_id="race-task", attempt_id="race-attempt", gid=gid, role="content", last_engine_state="active"))
        await db.commit()
    aria = FakeAria()
    aria.files = [{"path": str(output), "selected": "true"}]
    scheduler = scheduler_module.Scheduler(aria, Settings(download_root=root, private_root=tmp_path / "private"))
    scheduler.available = True
    try:
        await scheduler.recover()
        assert scheduler.active["race-task"] == gid
        async with sessions() as db:
            assert (await db.get(Task, "race-task")).pending_action == "cancel"
        await scheduler.tick()
        async with sessions() as db:
            task = await db.get(Task, "race-task")
            assert task.status == "completed"
            assert task.pending_action is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_restarts_dead_engine_and_recovers_task(tmp_path, monkeypatch):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'engine-death.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "downloads"
    root.mkdir()
    old_gid = "1234567890abcdef"
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="dead-engine-task", kind="general", source_type="http", source_url="https://example.test/file", source_fingerprint="dead-engine", title="file", status="downloading", attempt_count=1, download_subdir="general", task_directory_key="general/dead-engine-task", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(TaskAttempt(id="dead-attempt", task_id="dead-engine-task", cycle=0, ordinal=1, engine="aria2", status="running", engine_ref=old_gid))
        db.add(Aria2Binding(task_id="dead-engine-task", attempt_id="dead-attempt", gid=old_gid, role="content", last_engine_state="active"))
        await db.commit()

    class RestartedAria(FakeAria):
        def __init__(self):
            super().__init__()
            self.is_running = False
            self.starts = 0

        async def start(self):
            self.starts += 1
            self.is_running = True
            return {"version": "test"}

        async def tell_status(self, gid):
            if gid == old_gid:
                raise RuntimeError("old engine GID was lost")
            return {"status": "active", "completedLength": "0", "totalLength": "5"}

    aria = RestartedAria()
    scheduler = scheduler_module.Scheduler(aria, Settings(download_root=root, private_root=tmp_path / "private"))
    scheduler.available = True
    scheduler.active["dead-engine-task"] = old_gid
    try:
        await scheduler.tick()
        assert aria.starts == 1
        assert len(aria.added) == 1
        assert scheduler.active["dead-engine-task"] != old_gid
        async with sessions() as db:
            assert (await db.get(Task, "dead-engine-task")).status == "downloading"
            assert len((await db.scalars(select(TaskAttempt).where(TaskAttempt.task_id == "dead-engine-task"))).all()) == 2
    finally:
        await database.dispose()
