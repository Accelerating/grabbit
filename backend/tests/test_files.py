from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import files as files_api
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import FileRecord, Task, User
from app.db.session import get_session
from app.main import app
from app.services import file_operations, media_probe


@pytest.mark.asyncio
async def test_authenticated_file_browse_and_range(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    (root / "general").mkdir(parents=True)
    content = b"0123456789" * 1024
    (root / "general" / "archive.bin").write_bytes(content)
    (root / "general" / "hidden.part").write_bytes(b"partial")
    (root / "general" / "escape").symlink_to(tmp_path / "outside")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'files.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(files_api, "SessionLocal", sessions)
    monkeypatch.setattr(file_operations, "SessionLocal", sessions)
    monkeypatch.setattr(media_probe, "SessionLocal", sessions)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(User(username="admin", password_hash=hash_password("123456")))
        await db.commit()

    async def test_session():
        async with sessions() as db:
            yield db

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'files.db'}",
        download_root=root, private_root=tmp_path / "private", public_origin="http://testserver",
    )
    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/v1/files?directory=general")).status_code == 401
            login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"}, headers={"Origin": "http://testserver"})
            assert login.status_code == 200
            listing = await client.get("/api/v1/files?directory=general")
            assert listing.status_code == 200
            items = listing.json()["items"]
            assert [item["name"] for item in items] == ["archive.bin"]
            file_id = items[0]["id"]
            partial = await client.get(f"/api/v1/files/{file_id}/content", headers={"Range": "bytes=10-19"})
            assert partial.status_code == 206
            assert partial.content == content[10:20]
            assert (await client.head(f"/api/v1/files/{file_id}/content")).status_code == 200
            assert (await client.get("/api/v1/files?directory=../")).status_code == 422
            (root / "general" / "clip.mp4").write_bytes(b"media")
            (root / "general" / "clip.en.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n")
            async with sessions() as db:
                db.add(Task(id="subtitle-task", kind="video", source_type="video", source_url="https://www.youtube.com/watch?v=test", source_fingerprint="subtitle", title="Clip", status="completed", download_subdir="general", task_directory_key="general/subtitle-task", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
                db.add(FileRecord(id="media-id", task_id="subtitle-task", relative_path="general/clip.mp4", display_name="clip.mp4", mime_type="video/mp4", kind="video", availability="present", is_complete=True))
                db.add(FileRecord(id="subtitle-id", task_id="subtitle-task", relative_path="general/clip.en.vtt", display_name="clip.en.vtt", mime_type="text/vtt", kind="other", availability="present", is_complete=True))
                await db.commit()
            tracks = await client.get("/api/v1/files/media-id/subtitles")
            assert tracks.status_code == 200 and tracks.json()["items"][0]["language"] == "en"
            subtitle = await client.get("/api/v1/files/media-id/subtitles/subtitle-id")
            assert subtitle.status_code == 200 and subtitle.text.startswith("WEBVTT")
            assert (await client.get("/api/v1/files/media-id/subtitles/not-related")).status_code == 404
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}
            async def fake_inspect(_path):
                return {"duration_seconds": 12.5, "format_name": "mov,mp4", "streams": [{"codec_type": "video", "codec_name": "h264", "width": 320, "height": 180}]}
            monkeypatch.setattr(media_probe, "inspect_media", fake_inspect)
            queued = await client.post("/api/v1/files/media-id/probe", headers=headers)
            assert queued.status_code == 202 and queued.json()["probe_status"] == "queued"
            await media_probe.MediaProbeWorker(settings).tick()
            probed = (await client.get("/api/v1/files/media-id")).json()
            assert probed["probe_status"] == "completed" and probed["media"]["streams"][0]["codec_name"] == "h264"
            (root / "general" / "clip.mp4").write_bytes(b"changed-media")
            changed_media = (await client.get("/api/v1/files/media-id")).json()
            assert changed_media["probe_status"] == "unprobed" and changed_media["media"] is None
            created_directory = await client.post("/api/v1/directories", json={"parent": "general", "name": "new-folder"}, headers=headers)
            assert created_directory.status_code == 201 and (root / "general" / "new-folder").is_dir()
            assert (await client.post("/api/v1/directories", json={"parent": "general", "name": "new-folder"}, headers=headers)).status_code == 409
            assert (await client.post("/api/v1/directories", json={"parent": "general", "name": "../escape"}, headers=headers)).status_code == 422
            preview = await client.post("/api/v1/files/deletion-preview", json={"file_ids": [file_id]}, headers=headers)
            assert preview.status_code == 200 and preview.json()["total_bytes"] == len(content)
            assert (await client.post("/api/v1/files/delete", json={"preview_id": preview.json()["preview_id"], "confirmed": False}, headers=headers)).status_code == 422
            (root / "general" / "archive.bin").write_bytes(content + b"changed")
            stale = await client.post("/api/v1/files/delete", json={"preview_id": preview.json()["preview_id"], "confirmed": True}, headers=headers)
            assert stale.status_code == 409
            fresh = await client.post("/api/v1/files/deletion-preview", json={"file_ids": [file_id]}, headers=headers)
            deleted = await client.post("/api/v1/files/delete", json={"preview_id": fresh.json()["preview_id"], "confirmed": True}, headers=headers)
            assert deleted.status_code == 202 and deleted.json()["status"] == "queued"
            again = await client.post("/api/v1/files/delete", json={"preview_id": fresh.json()["preview_id"], "confirmed": True}, headers=headers)
            assert again.status_code == 202 and again.json()["id"] == deleted.json()["id"]
            await file_operations.FileOperationWorker(settings).tick()
            operation = await client.get(f"/api/v1/files/operations/{deleted.json()['id']}")
            assert operation.status_code == 200 and operation.json()["status"] == "completed"
            assert not (root / "general" / "archive.bin").exists()
            assert (await client.get(f"/api/v1/files/{file_id}/content")).status_code == 404
            (root / "general" / "first.bin").write_bytes(b"first")
            (root / "general" / "second.bin").write_bytes(b"second")
            listed = (await client.get("/api/v1/files?directory=general")).json()["items"]
            ids = {item["name"]: item["id"] for item in listed if item["type"] == "file"}
            batch_preview = await client.post("/api/v1/files/deletion-preview", json={"file_ids": [ids["first.bin"], ids["second.bin"]]}, headers=headers)
            batch = await client.post("/api/v1/files/delete", json={"preview_id": batch_preview.json()["preview_id"], "confirmed": True}, headers=headers)
            (root / "general" / "second.bin").write_bytes(b"replaced")
            await file_operations.FileOperationWorker(settings).tick()
            batch_result = (await client.get(f"/api/v1/files/operations/{batch.json()['id']}")).json()
            assert batch_result["status"] == "partial"
            assert batch_result["items"] == [{"id": ids["first.bin"], "status": "deleted"}, {"id": ids["second.bin"], "status": "changed"}]
            assert not (root / "general" / "first.bin").exists()
            assert (root / "general" / "second.bin").read_bytes() == b"replaced"
            nested = root / "general" / "new-folder"
            (nested / "inner").mkdir()
            (nested / "inner" / "item.bin").write_bytes(b"nested")
            async with sessions() as db:
                busy = Task(id="busy-task", kind="general", source_type="http", source_url="https://example.com/x",
                            source_fingerprint="busy", title="busy", status="queued", download_subdir="general/new-folder",
                            task_directory_key="general/new-folder/busy-task", progress_json="{}", selection_json="{}",
                            options_json="{}", warnings_json="[]")
                db.add(busy)
                await db.commit()
            blocked = await client.post("/api/v1/directories/deletion-preview", json={"directory": "general/new-folder"}, headers=headers)
            assert blocked.status_code == 409
            async with sessions() as db:
                busy = await db.get(Task, "busy-task")
                busy.status = "completed"
                await db.commit()
            directory_preview = await client.post("/api/v1/directories/deletion-preview", json={"directory": "general/new-folder"}, headers=headers)
            assert directory_preview.status_code == 200
            assert directory_preview.json()["file_count"] == 1 and directory_preview.json()["directory_count"] == 2
            (nested / "new.bin").write_bytes(b"new")
            changed = await client.post("/api/v1/files/delete", json={"preview_id": directory_preview.json()["preview_id"], "confirmed": True}, headers=headers)
            assert changed.status_code == 409
            (nested / "new.bin").unlink()
            directory_preview = await client.post("/api/v1/directories/deletion-preview", json={"directory": "general/new-folder"}, headers=headers)
            directory_job = await client.post("/api/v1/files/delete", json={"preview_id": directory_preview.json()["preview_id"], "confirmed": True}, headers=headers)
            assert directory_job.status_code == 202 and directory_job.json()["kind"] == "delete_directory"
            await file_operations.FileOperationWorker(settings).tick()
            result = (await client.get(f"/api/v1/files/operations/{directory_job.json()['id']}")).json()
            assert result["status"] == "completed" and len(result["items"]) == 3
            assert not nested.exists()
            unsafe = root / "general" / "unsafe-folder"
            unsafe.mkdir()
            unsafe.joinpath("outside-link").symlink_to(tmp_path)
            unsafe_preview = await client.post("/api/v1/directories/deletion-preview", json={"directory": "general/unsafe-folder"}, headers=headers)
            assert unsafe_preview.status_code == 409 and unsafe.is_dir()
            race = root / "general" / "race-folder"
            race.mkdir()
            (race / "listed.bin").write_bytes(b"listed")
            race_preview = await client.post("/api/v1/directories/deletion-preview", json={"directory": "general/race-folder"}, headers=headers)
            race_job = await client.post("/api/v1/files/delete", json={"preview_id": race_preview.json()["preview_id"], "confirmed": True}, headers=headers)
            (race / "new.bin").write_bytes(b"must remain")
            await file_operations.FileOperationWorker(settings).tick()
            race_result = (await client.get(f"/api/v1/files/operations/{race_job.json()['id']}")).json()
            assert race_result["status"] == "partial"
            assert (race / "new.bin").read_bytes() == b"must remain"
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
