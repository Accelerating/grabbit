from __future__ import annotations

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.paths import safe_subdirectory
from app.core.security import hash_password
from app.core.origin import origin_allowed
from app.api.auth import PasswordRequest
from app.core.task_state import TaskStatus, can_transition
from app.db.base import Base
from app.db.models import FileRecord, User
from app.db.session import get_session
from app.main import app
from app.api import system as system_api
from app.api import tasks as tasks_api


def test_safe_subdirectory(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    assert safe_subdirectory(root, "general/new") == root / "general/new"
    for bad in ("../other", "/etc", "a/../../b", "", "bad\x00name"):
        with pytest.raises(ApiError):
            safe_subdirectory(root, bad)
    (root / "escape").symlink_to(tmp_path)
    with pytest.raises(ApiError):
        safe_subdirectory(root, "escape/file")


def test_task_state_contract():
    assert can_transition(TaskStatus.QUEUED, TaskStatus.DOWNLOADING)
    assert can_transition(TaskStatus.PAUSED, TaskStatus.QUEUED)
    assert not can_transition(TaskStatus.COMPLETED, TaskStatus.DOWNLOADING)


def test_local_origin_follows_actual_loopback_port():
    def request(origin: str, host: str = "127.0.0.1:8765", client: str = "127.0.0.1") -> Request:
        return Request({
            "type": "http", "method": "POST", "path": "/api/v1/auth/login",
            "headers": [(b"host", host.encode()), (b"origin", origin.encode())],
            "client": (client, 45678), "scheme": "http", "server": ("127.0.0.1", 8765),
        })

    local = Settings(public_origin="http://localhost:8000", secure_cookies=False)
    assert origin_allowed(request("http://127.0.0.1:8765"), local)
    assert origin_allowed(request("http://localhost:8765", host="localhost:8765"), local)
    assert not origin_allowed(request("http://evil.test:8765"), local)
    assert not origin_allowed(request("http://127.0.0.1:9999"), local)
    assert not origin_allowed(request("http://127.0.0.1:8765", client="192.0.2.10"), local)
    production = Settings(public_origin="https://grabbit.example.com", secure_cookies=True)
    assert not origin_allowed(request("http://127.0.0.1:8765"), production)


def test_password_minimum_is_six():
    assert PasswordRequest(current_password="old", new_password="123456").new_password == "123456"
    with pytest.raises(ValueError):
        PasswordRequest(current_password="old", new_password="12345")


@pytest.mark.asyncio
async def test_login_session_csrf_and_logout(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(User(username="admin", password_hash=hash_password("correct-password")))
        await db.commit()

    async def test_session():
        async with sessions() as db:
            yield db

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        download_root=tmp_path / "downloads",
        private_root=tmp_path / "private",
        public_origin="http://testserver",
    )
    settings.download_root.mkdir()
    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(system_api, "SessionLocal", sessions)
    monkeypatch.setattr(tasks_api, "SessionLocal", sessions)
    monkeypatch.setattr(tasks_api, "get_settings", lambda: settings)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/v1/auth/session")).status_code == 401
            assert (await client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-password"})).status_code == 403
            logged_in = await client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "correct-password"},
                headers={"Origin": "http://testserver"},
            )
            assert logged_in.status_code == 200
            assert "httponly" in logged_in.headers["set-cookie"].lower()
            csrf = logged_in.json()["csrf_token"]
            refreshed = await client.get("/api/v1/auth/session")
            assert refreshed.status_code == 200
            assert refreshed.json()["csrf_token"] != csrf
            current = await client.get("/api/v1/settings")
            assert current.json()["editable"]["max_active_tasks"] == 1
            dependencies = await client.get("/api/v1/dependencies")
            assert dependencies.status_code == 200
            assert {"aria2", "yt-dlp", "ffmpeg", "ffprobe", "deno"} <= set(dependencies.json()["items"])
            changed = await client.patch(
                "/api/v1/settings", json={"revision": 0, "max_active_tasks": 2},
                headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]},
            )
            assert changed.status_code == 200
            assert changed.json()["revision"] == 1
            stale = await client.patch(
                "/api/v1/settings", json={"revision": 0, "max_active_tasks": 3},
                headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]},
            )
            assert stale.status_code == 409
            created = await client.post(
                "/api/v1/tasks/general",
                json={"sources": ["https://example.com/archive.zip"], "download_subdir": "general"},
                headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"], "Idempotency-Key": "one"},
            )
            assert created.status_code == 201, created.text
            assert created.json()["items"][0]["status"] == "queued"
            duplicate_request = await client.post(
                "/api/v1/tasks/general",
                json={"sources": ["https://example.com/archive.zip"], "download_subdir": "general"},
                headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"], "Idempotency-Key": "one"},
            )
            assert duplicate_request.json() == created.json()
            assert (await client.get("/api/v1/tasks?kind=general")).json()["items"][0]["id"] == created.json()["items"][0]["id"]
            queue = (await client.get("/api/v1/queue")).json()
            assert len(queue["items"]) == 1
            moved = await client.post(
                f"/api/v1/queue/{queue['items'][0]['id']}/move",
                json={"direction": "top", "revision": queue["revision"]},
                headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]},
            )
            assert moved.status_code == 200
            assert (await client.post(
                f"/api/v1/queue/{queue['items'][0]['id']}/move",
                json={"direction": "top", "revision": queue["revision"]},
                headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]},
            )).status_code == 409
            task_id = created.json()["items"][0]["id"]
            paused = await client.post(f"/api/v1/tasks/{task_id}/pause", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})
            assert paused.json()["status"] == "paused"
            assert (await client.get("/api/v1/queue")).json()["items"] == []
            resumed = await client.post(f"/api/v1/tasks/{task_id}/resume", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})
            assert resumed.json()["status"] == "queued"
            assert (await client.post(f"/api/v1/tasks/{task_id}/deletion-preview", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})).status_code == 409
            cancelled = await client.post(f"/api/v1/tasks/{task_id}/cancel", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})
            assert cancelled.json()["status"] == "cancelled"
            output = settings.download_root / "general" / task_id / "example.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"kept")
            async with sessions() as db:
                db.add(FileRecord(task_id=task_id, relative_path=f"general/{task_id}/example.mp4", display_name="example.mp4", size_bytes=4, availability="present", is_complete=True, kind="video"))
                await db.commit()
            preview = await client.post(f"/api/v1/tasks/{task_id}/deletion-preview", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})
            assert preview.status_code == 200 and preview.json()["files"][0]["name"] == "example.mp4"
            assert (await client.delete(f"/api/v1/tasks/{task_id}?revision=0", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})).status_code == 409
            assert (await client.delete(f"/api/v1/tasks/{task_id}?revision={preview.json()['task_revision']}", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})).status_code == 204
            assert output.read_bytes() == b"kept"
            async with sessions() as db:
                assert (await db.get(FileRecord, preview.json()["files"][0]["id"])).task_id is None
            assert (await client.post("/api/v1/auth/logout", headers={"Origin": "http://testserver", "X-CSRF-Token": csrf})).status_code == 403
            assert (await client.post("/api/v1/auth/logout", headers={"Origin": "http://testserver", "X-CSRF-Token": refreshed.json()["csrf_token"]})).status_code == 204
            assert (await client.get("/api/v1/auth/session")).status_code == 401
            missing = await client.get("/api/v1/not-real")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "NOT_FOUND"
        local_settings = Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
            download_root=tmp_path / "downloads", private_root=tmp_path / "private",
            public_origin="http://localhost:8000", secure_cookies=False,
        )
        app.dependency_overrides[get_settings] = lambda: local_settings
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8765") as local_client:
            local_login = await local_client.post(
                "/api/v1/auth/login", json={"username": "admin", "password": "correct-password"},
                headers={"Origin": "http://127.0.0.1:8765"},
            )
            assert local_login.status_code == 200, local_login.text
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()
