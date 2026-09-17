from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import torrents as torrent_api
from app.core.config import Settings
from app.db.base import Base
from app.db.models import Task, TaskSource, TorrentUpload, WorkItem, utcnow
from app.services.torrent import parse_torrent, persist_torrent_blob


def torrent() -> bytes:
    return b"d4:infod6:lengthi12e4:name9:video.mkv12:piece lengthi16384e6:pieces20:xxxxxxxxxxxxxxxxxxxxee"


@pytest.mark.asyncio
async def test_selected_torrent_creates_durable_queued_task(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'task.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'task.db'}",
        download_root=tmp_path / "downloads", private_root=tmp_path / "private",
        public_origin="http://testserver",
    )
    blob = torrent()
    parsed = parse_torrent(blob)
    key = persist_torrent_blob(settings.private_root, blob)
    async with sessions() as db:
        db.add(TorrentUpload(id="upload-1", blob_key=key, infohash=parsed.infohash, summary_json=json.dumps(parsed.summary), expires_at=utcnow() + timedelta(hours=1)))
        await db.commit()
    monkeypatch.setattr(torrent_api, "SessionLocal", sessions)
    monkeypatch.setattr(torrent_api, "get_settings", lambda: settings)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(event_bus=None)))
    try:
        response = await torrent_api.create_torrent_task(torrent_api.TorrentTaskCreate(torrent_id="upload-1", selected_indices=[1]), request, None)
        assert response["source_type"] == "torrent"
        async with sessions() as db:
            task = await db.get(Task, response["id"])
            source = await db.get(TaskSource, response["id"])
            assert task is not None and task.status == "queued"
            assert source is not None and source.torrent_blob_key == key
            assert json.loads(task.selection_json)["selected_indices"] == [1]
            assert await db.scalar(select(WorkItem.id).where(WorkItem.resource_id == task.id)) is not None
    finally:
        await engine.dispose()
