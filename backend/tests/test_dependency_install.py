from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import system as system_api
from app.core.config import Settings
from app.db.base import Base
from app.db.models import DependencyInstallJob, RuntimeControl
from app.services import dependency_install as install_service


@pytest.mark.asyncio
async def test_fixed_action_job_suspends_dispatch_and_restores_it(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'jobs.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(RuntimeControl(id=1, dispatch_suspended=False, revision=1))
        await db.commit()
    helper = tmp_path / "helper"
    helper.write_text("fixed helper")
    monkeypatch.setattr(system_api, "SessionLocal", sessions)
    monkeypatch.setattr(install_service, "SessionLocal", sessions)
    monkeypatch.setattr(system_api, "helper_available", lambda: True)
    with pytest.raises(system_api.ApiError):
        await system_api.create_install_job(system_api.InstallRequest(action="curl"), auth=None)
    created = await system_api.create_install_job(system_api.InstallRequest(action="aria2"), auth=None)
    assert created["status"] == "queued"
    with pytest.raises(system_api.ApiError):
        await system_api.create_install_job(system_api.InstallRequest(action="ffmpeg"), auth=None)

    class Engine:
        async def close(self):
            pass

    scheduler = SimpleNamespace(active={}, video_active={}, subtitle_active={}, available=True,
                                maintenance=False, engine=Engine(), _next_engine_retry=1,
                                settings=Settings(download_root=tmp_path), _lock=__import__("asyncio").Lock())
    worker = install_service.DependencyInstallWorker(scheduler)
    async def fake_helper(action):
        assert action == "aria2" and scheduler.maintenance
        return True, "installed", None
    monkeypatch.setattr(install_service, "run_helper", fake_helper)
    monkeypatch.setattr(install_service, "available_memory_bytes", lambda: 1024**3)
    monkeypatch.setattr(install_service.shutil, "disk_usage", lambda _: SimpleNamespace(free=2 * 1024**3))
    await worker.tick()
    async with sessions() as db:
        job = await db.scalar(select(DependencyInstallJob))
        control = await db.get(RuntimeControl, 1)
        assert job.status == "completed" and job.log_text == "installed"
        assert not control.dispatch_suspended and control.blocked_reason is None
    assert not scheduler.maintenance and scheduler._next_engine_retry == 0
    await engine.dispose()
