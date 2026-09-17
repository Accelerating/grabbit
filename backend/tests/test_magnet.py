from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import tasks as task_api
from app.core.config import Settings
from app.core.errors import ApiError
from app.db.base import Base
from app.db.models import Setting, Task, TaskSource, WorkItem
from app.services import scheduler as scheduler_module
from app.services.torrent import parse_torrent
from test_torrent_task import torrent


def test_normalize_magnet_hash_and_reject_invalid():
    infohash = "ab" * 20
    encoded = base64.b32encode(bytes.fromhex(infohash)).decode()
    assert task_api.normalize_magnet(f"magnet:?xt=urn:btih:{encoded}&dn=Sample")[1:] == (infohash, "Sample")
    with pytest.raises(ApiError):
        task_api.normalize_magnet("magnet:?dn=missing")


class FakeMagnetAria:
    def __init__(self):
        self.status = "active"
        self.metadata_calls = []
        self.torrent_calls = []

    async def add_magnet_metadata(self, uri, directory, gid):
        self.metadata_calls.append((uri, directory, gid))
        return gid

    async def add_torrent(self, blob, directory, gid, indices):
        self.torrent_calls.append((blob, directory, gid, indices))
        return gid

    async def unpause(self, gid):
        return gid

    async def tell_status(self, gid):
        return {"status": self.status, "followedBy": []}


@pytest.mark.asyncio
async def test_metadata_waits_for_selection_before_content(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'magnet.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    monkeypatch.setattr(task_api, "SessionLocal", sessions)
    root = tmp_path / "downloads"
    root.mkdir()
    blob = torrent()
    parsed = parse_torrent(blob)
    magnet = f"magnet:?xt=urn:btih:{parsed.infohash}"
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="magnet-task", kind="general", source_type="magnet", source_url=magnet, source_fingerprint=parsed.infohash, title="magnet", status="queued", download_subdir="general", task_directory_key="general/magnet-task", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(TaskSource(task_id="magnet-task", magnet_infohash=parsed.infohash, source_snapshot_json="{}"))
        db.add(WorkItem(work_type="metadata", resource_id="magnet-task", position=1))
        await db.commit()
    aria = FakeMagnetAria()
    scheduler = scheduler_module.Scheduler(aria, Settings(download_root=root, private_root=tmp_path / "private"))
    scheduler.available = True
    try:
        await scheduler.tick()
        assert len(aria.metadata_calls) == 1
        assert aria.torrent_calls == []
        candidate = root / "general" / "magnet-task" / f"{parsed.infohash}.torrent"
        candidate.write_bytes(blob)
        aria.status = "complete"
        await scheduler.tick()
        async with sessions() as db:
            task = await db.get(Task, "magnet-task")
            assert task.status == "awaiting_selection"
            revision = task.revision
            assert (await db.get(TaskSource, task.id)).torrent_blob_key
            assert await db.scalar(select(WorkItem.id).where(WorkItem.resource_id == task.id)) is None
        assert aria.torrent_calls == []
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(event_bus=None)))
        response = await task_api.select_magnet_files("magnet-task", task_api.SelectionRequest(selected_indices=[1], metadata_revision=revision), request, None)
        assert response["status"] == "queued"
        aria.status = "active"
        await scheduler.tick()
        assert len(aria.torrent_calls) == 1
        assert aria.torrent_calls[0][3] == [1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_magnet_create_queues_metadata_not_content(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'create.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(task_api, "SessionLocal", sessions)
    monkeypatch.setattr(task_api, "get_settings", lambda: Settings(download_root=tmp_path / "downloads", private_root=tmp_path / "private"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(event_bus=None)))
    auth = SimpleNamespace(user_id="admin")
    infohash = "ab" * 20
    try:
        response = await task_api.create_general(task_api.GeneralCreate(sources=[f"magnet:?xt=urn:btih:{infohash}&dn=Example"]), auth, request, None)
        task_id = response["items"][0]["id"]
        async with sessions() as db:
            assert (await db.get(Task, task_id)).source_type == "magnet"
            assert (await db.get(TaskSource, task_id)).magnet_infohash == infohash
            assert (await db.scalar(select(WorkItem).where(WorkItem.resource_id == task_id))).work_type == "metadata"
    finally:
        await engine.dispose()
