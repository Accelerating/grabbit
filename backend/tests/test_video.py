from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import tasks as tasks_api
from app.api import video as video_api
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import CookieProfile, FileRecord, Setting, Task, TaskAttempt, User, VideoAnalysisJob, WorkItem, utcnow
from app.db.session import get_session
from app.main import app
from app.services import scheduler as scheduler_module
from app.services.cookies import persist_cookie_blob
from app.services.video import _collection_entry_url, probe_media, resolve_collection_page, summarize_video, video_site
from app.services.video_analysis import VideoAnalysisWorker


def test_video_site_restricts_hosts_and_credentials():
    assert video_site("https://www.youtube.com/watch?v=test") == "youtube"
    assert video_site("https://www.bilibili.com/video/BV123") == "bilibili"
    for url in ("file:///etc/passwd", "https://youtube.com.evil.test/watch", "https://user:pass@youtube.com/watch", "http://localhost/private"):
        with pytest.raises(ApiError):
            video_site(url)


def test_structured_video_progress_uses_current_stream_scope():
    from app.services.scheduler import parse_video_progress_line
    progress = parse_video_progress_line(b'GRABBIT_PROGRESS {"downloaded_bytes":512,"total_bytes":1024,"speed":128,"eta":4}')
    assert progress == {"scope": "current_stream", "percent": 50.0, "downloaded_bytes": 512, "total_bytes": 1024,
                        "total_is_estimate": False, "speed_bps": 128, "eta_seconds": 4}
    assert parse_video_progress_line(b'[download] 50%') is None
    assert parse_video_progress_line(b'GRABBIT_PROGRESS {"downloaded_bytes":true}') is None


def test_video_summary_excludes_secrets_and_bounded_formats():
    raw = {
        "title": "Example", "duration": 62, "webpage_url": "https://secret.example/?token=secret",
        "formats": [{"format_id": "137", "height": 1080, "ext": "mp4", "vcodec": "avc1", "acodec": "none", "url": "https://secret.example/stream"}],
        "subtitles": {"en": [{"url": "https://secret.example/subtitle"}], "live_chat": []},
    }
    result = summarize_video(raw, "youtube")
    assert result["title"] == "Example" and result["formats"][0]["id"] == "137"
    assert result["subtitle_languages"] == ["en"]
    assert "secret" not in str(result)
    with pytest.raises(ApiError):
        summarize_video({"_type": "playlist"}, "youtube")


def test_collection_entry_urls_are_site_scoped():
    assert _collection_entry_url({"id": "abc123XYZ09"}, "youtube", "https://www.youtube.com/playlist?list=x", 1) == "https://www.youtube.com/watch?v=abc123XYZ09"
    assert _collection_entry_url({"id": "BV1VWVz6TELx"}, "bilibili", "https://www.bilibili.com/video/BV1VWVz6TELx", 2) == "https://www.bilibili.com/video/BV1VWVz6TELx?p=2"
    assert _collection_entry_url({"webpage_url": "https://evil.example/x"}, "youtube", "https://www.youtube.com/playlist?list=x", 1) is None


@pytest.mark.asyncio
async def test_flat_collection_parser_bounds_page(tmp_path, monkeypatch):
    from app.services import video as video_module
    tool = tmp_path / "flat-yt-dlp"
    tool.write_text('#!/bin/sh\nprintf "%s\\n" \'{"playlist_id":"demo","playlist_title":"A list","playlist_index":1,"id":"abc123XYZ09","title":"First"}\' \'{"playlist_id":"demo","playlist_index":2,"id":"def456XYZ09","title":"Second"}\'\n')
    tool.chmod(0o755)
    monkeypatch.setattr(video_module.shutil, "which", lambda _name: str(tool))
    result = await resolve_collection_page("https://www.youtube.com/playlist?list=demo", "youtube", None, 1, 100)
    assert result["collection"] and result["title"] == "A list"
    assert [item["ordinal"] for item in result["items"]] == [1, 2]
    assert result["next_index"] is None
    assert result["items"][0]["source_url"] == "https://www.youtube.com/watch?v=abc123XYZ09"


@pytest.mark.asyncio
async def test_persistent_analysis_recovers_and_exposes_result(tmp_path, monkeypatch):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'analysis.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'analysis.db'}", download_root=tmp_path, private_root=tmp_path, public_origin="http://testserver")
    async with sessions() as db:
        db.add(User(username="admin", password_hash=hash_password("123456")))
        db.add(VideoAnalysisJob(id="recover", url="https://www.youtube.com/watch?v=test", site_key="youtube", status="running"))
        await db.commit()
    async def override_session():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(video_api, "SessionLocal", sessions)
    from app.services import video_analysis as analysis_module
    monkeypatch.setattr(analysis_module, "SessionLocal", sessions)
    async def fake_resolve(url, site, cookie_file):
        assert site == "youtube" and cookie_file is None
        return {"site": site, "title": "Demo", "formats": [], "subtitle_languages": []}
    monkeypatch.setattr(analysis_module, "resolve_video", fake_resolve)
    async def no_collection(_url, _site, _cookie_file, _start):
        return None
    monkeypatch.setattr(analysis_module, "resolve_collection_page", no_collection)
    worker = VideoAnalysisWorker(settings)
    try:
        await worker.start()
        await worker.close()
        async with sessions() as db:
            assert (await db.get(VideoAnalysisJob, "recover")).status == "queued"
        await worker.tick()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"}, headers={"Origin": "http://testserver"})
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}
            recovered = await client.get("/api/v1/video/analyses/recover")
            assert recovered.status_code == 200 and recovered.json()["result"]["title"] == "Demo"
            queued = await client.post("/api/v1/video/analyses", json={"url": "https://www.youtube.com/watch?v=next"}, headers=headers)
            assert queued.status_code == 202 and queued.json()["status"] == "queued"
            await worker.tick()
            finished = await client.get(f"/api/v1/video/analyses/{queued.json()['id']}")
            assert finished.json()["status"] == "completed"
            cancelled = await client.post("/api/v1/video/analyses", json={"url": "https://www.youtube.com/watch?v=cancel"}, headers=headers)
            stopped = await client.post(f"/api/v1/video/analyses/{cancelled.json()['id']}/cancel", headers=headers)
            assert stopped.status_code == 200 and stopped.json()["status"] == "cancelled"
            await worker.tick()
            assert (await client.get(f"/api/v1/video/analyses/{cancelled.json()['id']}")).json()["status"] == "cancelled"
            old = VideoAnalysisJob(id="old-analysis", url="https://www.youtube.com/watch?v=old", site_key="youtube", status="completed", result_json='{"title":"old"}', created_at=utcnow() - timedelta(days=2))
            async with sessions() as db:
                db.add(old)
                await db.commit()
            worker.last_expire_check = 0
            await worker.expire_old()
            expired = (await client.get("/api/v1/video/analyses/old-analysis")).json()
            assert expired["status"] == "expired" and expired["result"] is None
            started = asyncio.Event()
            async def slow_resolve(_url, _site, _cookie_file):
                started.set()
                await asyncio.Event().wait()
            monkeypatch.setattr(analysis_module, "resolve_video", slow_resolve)
            running = await client.post("/api/v1/video/analyses", json={"url": "https://www.youtube.com/watch?v=running"}, headers=headers)
            app.state.analysis_worker = worker
            tick = asyncio.create_task(worker.tick())
            await asyncio.wait_for(started.wait(), 2)
            stopped = await client.post(f"/api/v1/video/analyses/{running.json()['id']}/cancel", headers=headers)
            await asyncio.wait_for(tick, 2)
            assert stopped.json()["status"] == "cancelled"
            assert (await client.get(f"/api/v1/video/analyses/{running.json()['id']}")).json()["status"] == "cancelled"
            async def collection_page(_url, _site, _cookie_file, start):
                if start > 1:
                    return {"site": "youtube", "collection": True, "title": "Collection", "items": [], "next_index": None}
                return {"site": "youtube", "collection": True, "title": "Demo playlist", "items": [
                    {"ordinal": 1, "source_url": "https://www.youtube.com/watch?v=first01", "title": "First", "duration_seconds": 12},
                    {"ordinal": 2, "source_url": "https://www.youtube.com/watch?v=second02", "title": "Second", "duration_seconds": None},
                ], "next_index": 3}
            monkeypatch.setattr(analysis_module, "resolve_collection_page", collection_page)
            monkeypatch.setattr(video_api, "get_settings", lambda: settings)
            collection = await client.post("/api/v1/video/analyses", json={"url": "https://www.youtube.com/playlist?list=demo"}, headers=headers)
            await worker.tick()
            collection_id = collection.json()["id"]
            result = (await client.get(f"/api/v1/video/analyses/{collection_id}")).json()
            assert result["status"] == "completed" and result["result"]["collection"] is True
            items = (await client.get(f"/api/v1/video/analyses/{collection_id}/items")).json()["items"]
            assert len(items) == 2 and items[0]["title"] == "First"
            created = await client.post(f"/api/v1/video/analyses/{collection_id}/tasks", json={"item_ids": [item["id"] for item in items]}, headers=headers)
            assert created.status_code == 201 and len(created.json()["items"]) == 2
            assert created.json()["items"][0]["group_id"] == created.json()["group_id"]
            duplicate = await client.post(f"/api/v1/video/analyses/{collection_id}/tasks", json={"item_ids": [items[0]["id"]]}, headers=headers)
            assert duplicate.status_code == 409
            more = await client.post(f"/api/v1/video/analyses/{collection_id}/next-page", headers=headers)
            assert more.status_code == 202
            await worker.tick()
            final = (await client.get(f"/api/v1/video/analyses/{collection_id}")).json()
            assert final["result"]["title"] == "Demo playlist" and final["next_index"] is None
    finally:
        if getattr(app.state, "analysis_worker", None) is worker:
            del app.state.analysis_worker
        await worker.close()
        app.dependency_overrides.clear()
        await database.dispose()


@pytest.mark.asyncio
async def test_ffprobe_rejects_invalid_media_and_accepts_audio(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are not installed")
    invalid = tmp_path / "invalid.mp4"
    invalid.write_bytes(b"not a movie")
    assert not await probe_media(invalid)
    audio = tmp_path / "tone.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2", "-y", str(audio)], check=True, timeout=15)
    assert await probe_media(audio)


@pytest.mark.asyncio
async def test_video_task_uses_shared_queue_and_indexes_output(tmp_path, monkeypatch):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'video.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    root = tmp_path / "downloads"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'video.db'}", download_root=root, private_root=private, public_origin="http://testserver")
    profile_id = str(uuid4())
    cookie_key = persist_cookie_blob(private, profile_id, 1, b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tsecret\n")
    async with sessions() as db:
        db.add(User(username="admin", password_hash=hash_password("123456")))
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(CookieProfile(id=profile_id, name="YT", site_key="youtube", domain_scope_json='["youtube.com"]', secret_file_key=cookie_key, content_version=1, entry_count=1))
        await db.commit()
    async def override_session():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(video_api, "SessionLocal", sessions)
    monkeypatch.setattr(video_api, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks_api, "SessionLocal", sessions)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    async def valid_media(_path):
        return True
    monkeypatch.setattr(scheduler_module, "probe_media", valid_media)
    tool = tmp_path / "fake-yt-dlp"
    tool.write_text('#!/bin/sh\nfor arg in "$@"; do [ "$arg" = "--skip-download" ] && exit 1; done\nwhile [ "$1" != "--paths" ]; do shift; done\nshift\nmkdir -p "$1"\nprintf fake > "$1/done.mp4"\n')
    tool.chmod(0o755)
    monkeypatch.setattr(scheduler_module.shutil, "which", lambda name: str(tool) if name == "yt-dlp" else None)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"}, headers={"Origin": "http://testserver"})
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}
            body = {"url": "https://www.youtube.com/watch?v=test", "title": "Demo", "mode": "video", "download_subdir": "videos", "cookie_profile_id": profile_id, "subtitle_languages": ["en", "zh-Hans"]}
            created = await client.post("/api/v1/video/tasks", json=body, headers=headers)
            assert created.status_code == 201, created.text
            task_id = created.json()["id"]
            invalid_languages = await client.post("/api/v1/video/tasks", json={**body, "subtitle_languages": ["en,--exec=bad"]}, headers=headers)
            assert invalid_languages.status_code == 422
            assert (await client.post("/api/v1/video/tasks", json=body, headers=headers)).status_code == 409
            cleared = await client.patch(f"/api/v1/tasks/{task_id}/cookie", json={"revision": created.json()["revision"], "cookie_profile_id": None}, headers=headers)
            assert cleared.status_code == 200 and cleared.json()["cookie_profile_id"] is None
            assert (await client.patch(f"/api/v1/tasks/{task_id}/cookie", json={"revision": created.json()["revision"], "cookie_profile_id": profile_id}, headers=headers)).status_code == 409
            restored = await client.patch(f"/api/v1/tasks/{task_id}/cookie", json={"revision": cleared.json()["revision"], "cookie_profile_id": profile_id}, headers=headers)
            assert restored.status_code == 200 and restored.json()["cookie_profile_id"] == profile_id
            async with sessions() as db:
                queued = await db.scalar(select(WorkItem).where(WorkItem.resource_id == task_id))
                assert queued.work_type == "video"
                assert json.loads((await db.get(Task, task_id)).options_json)["subtitle_languages"] == ["en", "zh-Hans"]
        class NoAria:
            is_running = False
            async def start(self):
                raise FileNotFoundError
        scheduler = scheduler_module.Scheduler(NoAria(), settings)
        scheduler._next_engine_retry = float("inf")
        await scheduler.tick()
        assert task_id in scheduler.video_active
        snapshot = scheduler.video_active[task_id][2]
        assert snapshot and snapshot.read_bytes().endswith(b"secret\n")
        assert os.stat(snapshot).st_mode & 0o777 == 0o600
        process = scheduler.video_active[task_id][0]
        await process.wait()
        await scheduler.tick()
        assert task_id in scheduler.video_active
        await scheduler.video_active[task_id][0].wait()
        await scheduler.tick()
        async with sessions() as db:
            task = await db.get(Task, task_id)
            assert task.status == "completed"
            assert json.loads(task.warnings_json) == ["SUBTITLE_FAILED"]
            assert (await db.scalar(select(TaskAttempt.cookie_version).where(TaskAttempt.task_id == task_id))) == 1
            assert (root / "videos" / task_id / "done.mp4").read_bytes() == b"fake"
            assert not snapshot.exists()
        subtitle_tool = tmp_path / "fake-subtitle-yt-dlp"
        subtitle_tool.write_text('#!/bin/sh\nwhile [ "$1" != "--paths" ]; do shift; done\nshift\nprintf "WEBVTT\\n" > "$1/retry.en.vtt"\n')
        subtitle_tool.chmod(0o755)
        monkeypatch.setattr(scheduler_module.shutil, "which", lambda name: str(subtitle_tool) if name == "yt-dlp" else None)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"}, headers={"Origin": "http://testserver"})
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}
            retry = await client.post(f"/api/v1/tasks/{task_id}/retry-subtitles", headers=headers)
            assert retry.status_code == 202 and retry.json()["status"] == "queued"
            duplicate_retry = await client.post(f"/api/v1/tasks/{task_id}/retry-subtitles", headers=headers)
            assert duplicate_retry.json()["id"] == retry.json()["id"]
        await scheduler.tick()
        assert task_id in scheduler.subtitle_active
        await scheduler.subtitle_active[task_id][0].wait()
        await scheduler.tick()
        async with sessions() as db:
            task = await db.get(Task, task_id)
            assert task.status == "completed" and task.phase is None and json.loads(task.warnings_json) == []
            assert (await db.scalar(select(FileRecord).where(FileRecord.relative_path == f"videos/{task_id}/retry.en.vtt"))) is not None
    finally:
        app.dependency_overrides.clear()
        await database.dispose()


@pytest.mark.asyncio
async def test_video_stop_releases_slot_after_process_exit(tmp_path, monkeypatch):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stop.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "downloads"
    root.mkdir()
    private = tmp_path / "private"
    private.mkdir()
    tool = tmp_path / "slow-yt-dlp"
    tool.write_text("#!/bin/sh\nsleep 30\n")
    tool.chmod(0o755)
    monkeypatch.setattr(scheduler_module.shutil, "which", lambda name: str(tool) if name == "yt-dlp" else None)
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="slow-video", kind="video", source_type="video", source_url="https://www.youtube.com/watch?v=test", source_fingerprint="slow", title="Slow", status="queued", download_subdir="videos", task_directory_key="videos/slow-video", progress_json="{}", selection_json="{}", options_json='{"mode":"video","max_height":1080}', warnings_json="[]"))
        db.add(WorkItem(work_type="video", resource_id="slow-video", position=1))
        await db.commit()
    class NoAria:
        is_running = False
    scheduler = scheduler_module.Scheduler(NoAria(), Settings(download_root=root, private_root=private))
    scheduler._next_engine_retry = float("inf")
    try:
        await scheduler.tick()
        assert "slow-video" in scheduler.video_active
        async with sessions() as db:
            task = await db.get(Task, "slow-video")
            task.pending_action = "stop"
            await db.commit()
        await scheduler.tick()
        assert "slow-video" not in scheduler.video_active
        async with sessions() as db:
            assert (await db.get(Task, "slow-video")).status == "stopped"
    finally:
        await scheduler.close()
        await database.dispose()
