from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import cookies as cookie_api
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import Task, User
from app.db.session import get_session
from app.main import app
from app.services.cookies import CookieValidationError, parse_cookie_text


def cookie_text(value: str = "secret-value") -> str:
    return f"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\t{value}\n"


def test_cookie_parser_checks_scope_and_shape():
    assert parse_cookie_text(cookie_text().encode(), ("youtube.com",)).entry_count == 1
    with pytest.raises(CookieValidationError):
        parse_cookie_text(cookie_text().encode(), ("bilibili.com",))
    with pytest.raises(CookieValidationError):
        parse_cookie_text(b"not a cookie", ("youtube.com",))


@pytest.mark.asyncio
async def test_cookie_api_private_versions_defaults_and_reference_guard(tmp_path, monkeypatch):
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cookies.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(User(username="admin", password_hash=hash_password("123456")))
        await db.commit()
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'cookies.db'}", download_root=tmp_path / "downloads", private_root=tmp_path / "private", public_origin="http://testserver")
    monkeypatch.setattr(cookie_api, "SessionLocal", sessions)
    monkeypatch.setattr(cookie_api, "get_settings", lambda: settings)

    async def override_session():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get("/api/v1/cookies")).status_code == 401
            login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"}, headers={"Origin": "http://testserver"})
            assert login.status_code == 200
            headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}
            created = await client.post("/api/v1/cookies", data={"name": "YouTube test", "site_key": "youtube", "domains": "youtube.com", "text": cookie_text()}, headers=headers)
            assert created.status_code == 201, created.text
            profile = created.json()
            assert profile["entry_count"] == 1 and profile["content_version"] == 1
            assert "secret" not in created.text and "private" not in created.text
            profile_id = profile["id"]
            mismatch = await client.post("/api/v1/video/resolve", json={"url": "https://www.bilibili.com/video/BV123", "cookie_profile_id": profile_id}, headers=headers)
            assert mismatch.status_code == 422 and mismatch.json()["error"]["code"] == "COOKIE_SITE_MISMATCH"
            old_blob = settings.private_root / "cookies" / f"{profile_id}.1.txt"
            assert old_blob.read_text() == cookie_text()
            assert os.stat(old_blob).st_mode & 0o777 == 0o600
            assert os.stat(old_blob.parent).st_mode & 0o777 == 0o700
            listing = await client.get("/api/v1/cookies")
            assert listing.status_code == 200
            assert len(listing.json()["items"]) == 1 and "secret" not in listing.text
            default = await client.put("/api/v1/cookies/defaults/youtube", json={"cookie_profile_id": profile_id}, headers=headers)
            assert default.status_code == 200
            assert (await client.get("/api/v1/cookies")).json()["items"][0]["is_default"] is True
            replaced = await client.put(f"/api/v1/cookies/{profile_id}/content", data={"text": cookie_text("new-secret")}, headers=headers)
            assert replaced.status_code == 200 and replaced.json()["content_version"] == 2
            assert old_blob.read_text() == cookie_text()
            assert (settings.private_root / "cookies" / f"{profile_id}.2.txt").read_text() == cookie_text("new-secret")
            async with sessions() as db:
                db.add(Task(id="referencing-task", kind="video", source_type="video", source_fingerprint="ref", title="ref", status="queued", download_subdir="videos", task_directory_key="videos/referencing-task", cookie_profile_id=profile_id, progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
                await db.commit()
            blocked = await client.delete(f"/api/v1/cookies/{profile_id}", headers=headers)
            assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "COOKIE_IN_USE"
            async with sessions() as db:
                task = await db.get(Task, "referencing-task")
                task.status = "completed"
                await db.commit()
            deleted = await client.delete(f"/api/v1/cookies/{profile_id}", headers=headers)
            assert deleted.status_code == 204
            assert not old_blob.exists()
            assert (await client.get("/api/v1/cookies")).json()["items"] == []
            async with sessions() as db:
                task = await db.get(Task, "referencing-task")
                assert task.cookie_profile_id is None and task.cookie_name_snapshot == "YouTube test"
    finally:
        app.dependency_overrides.clear()
        await database.dispose()
