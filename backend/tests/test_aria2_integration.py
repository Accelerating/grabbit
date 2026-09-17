"""Run explicitly with GRABBIT_REAL_ARIA2_TEST=1 when aria2c is installed."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import secrets
import shutil
import socket
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.engines.aria2 import Aria2Engine
from app.api import tasks as task_api
from app.api import torrents as torrent_api
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import FileRecord, Setting, Task, TaskAttempt, User, WorkItem
from app.db.session import get_session
from app.main import app
from app.services import scheduler as scheduler_module
from app.services.torrent import parse_torrent
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import select
from test_torrent import bencode
from test_torrent_task import torrent


@asynccontextmanager
async def local_bt_peer(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        peer_port = probe.getsockname()[1]
    peers = socket.inet_aton("127.0.0.1") + peer_port.to_bytes(2, "big")

    class Tracker(BaseHTTPRequestHandler):
        def do_GET(self):
            body = bencode({b"interval": 1, b"peers": peers})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    tracker = ThreadingHTTPServer(("127.0.0.1", 0), Tracker)
    thread = threading.Thread(target=tracker.serve_forever, daemon=True)
    thread.start()
    payload = b"grabbit-app-peer-test" * 2048
    piece_length = 16384
    pieces = b"".join(hashlib.sha1(payload[start:start + piece_length]).digest() for start in range(0, len(payload), piece_length))
    announce = f"http://127.0.0.1:{tracker.server_port}/announce".encode()
    blob = bencode({b"announce": announce, b"info": {b"length": len(payload), b"name": b"app-fixture.bin", b"piece length": piece_length, b"pieces": pieces}})
    infohash = parse_torrent(blob).infohash
    seed_dir = tmp_path / "seed-app"
    seed_dir.mkdir()
    (seed_dir / "app-fixture.bin").write_bytes(payload)
    torrent_file = tmp_path / "app-fixture.torrent"
    torrent_file.write_bytes(blob)
    seed_config = tmp_path / "app-seed.conf"
    seed_config.write_text("")
    seeder = await asyncio.create_subprocess_exec(
        "aria2c", f"--conf-path={seed_config}", f"--dir={seed_dir}",
        f"--listen-port={peer_port}", "--enable-dht=false", "--bt-enable-lpd=false",
        "--seed-time=2", "--seed-ratio=100", "--check-integrity=true", "--file-allocation=none",
        str(torrent_file), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.sleep(1)
        if seeder.returncode is not None:
            raise AssertionError((await seeder.stderr.read()).decode(errors="replace"))
        magnet = f"magnet:?xt=urn:btih:{infohash}&tr=http%3A%2F%2F127.0.0.1%3A{tracker.server_port}%2Fannounce"
        yield blob, payload, magnet
    finally:
        if seeder.returncode is None:
            seeder.terminate()
            try:
                await asyncio.wait_for(seeder.wait(), 5)
            except asyncio.TimeoutError:
                seeder.kill()
                await seeder.wait()
        tracker.shutdown()
        tracker.server_close()
        thread.join(timeout=2)


class SlowHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        path = self.translate_path(self.path)
        file = open(path, "rb")
        size = os.fstat(file.fileno()).st_size
        range_header = self.headers.get("Range")
        self.range_end = size - 1
        if range_header and range_header.startswith("bytes="):
            start_text, _, end_text = range_header[6:].partition("-")
            start = int(start_text)
            end = min(int(end_text), size - 1) if end_text else size - 1
            file.seek(start)
            self.range_end = end
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
        else:
            self.send_response(200)
            self.send_header("Content-Length", str(size))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return file

    def copyfile(self, source, outputfile):
        while source.tell() <= self.range_end:
            chunk = source.read(min(16 * 1024, self.range_end - source.tell() + 1))
            if not chunk:
                break
            try:
                outputfile.write(chunk)
                outputfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.01)


class SlowNoRangeHandler(SlowHandler):
    def send_head(self):
        path = self.translate_path(self.path)
        file = open(path, "rb")
        size = os.fstat(file.fileno()).st_size
        self.range_end = size - 1
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        return file


@pytest.mark.asyncio
async def test_real_local_http_download(tmp_path):
    if os.environ.get("GRABBIT_REAL_ARIA2_TEST") != "1":
        pytest.skip("set GRABBIT_REAL_ARIA2_TEST=1 for real engine integration")
    if not shutil.which("aria2c"):
        pytest.skip("aria2c is not installed")
    source = tmp_path / "source"
    source.mkdir()
    (source / "fixture.bin").write_bytes(b"grabbit-real-aria2" * 1024)
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(source))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    engine = Aria2Engine(state_dir=tmp_path / "private", download_root=downloads)
    try:
        await engine.start()
        gid = secrets.token_hex(8)
        task_dir = downloads / "task"
        await engine.add_uri(f"http://127.0.0.1:{server.server_port}/fixture.bin", task_dir, gid)
        assert (await engine.tell_status(gid))["status"] == "paused"
        await engine.unpause(gid)
        for _ in range(100):
            status = await engine.tell_status(gid)
            state = status["status"]
            if state in {"complete", "error"}:
                break
            await asyncio.sleep(0.1)
        assert state == "complete", {key: status.get(key) for key in ("status", "errorCode", "errorMessage")}
        assert (task_dir / "fixture.bin").read_bytes() == (source / "fixture.bin").read_bytes()
    finally:
        await engine.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_real_pause_and_resume(tmp_path):
    if os.environ.get("GRABBIT_REAL_ARIA2_TEST") != "1" or not shutil.which("aria2c"):
        pytest.skip("requires GRABBIT_REAL_ARIA2_TEST=1 and aria2c")
    source = tmp_path / "source"
    source.mkdir()
    payload = b"grabbit-pause-resume" * (1024 * 128)
    (source / "slow.bin").write_bytes(payload)
    handler = functools.partial(SlowHandler, directory=str(source))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    engine = Aria2Engine(state_dir=tmp_path / "private", download_root=downloads)
    try:
        await engine.start()
        gid = secrets.token_hex(8)
        task_dir = downloads / "task"
        await engine.add_uri(f"http://127.0.0.1:{server.server_port}/slow.bin", task_dir, gid)
        await engine.unpause(gid)
        await asyncio.sleep(0.1)
        await engine.pause(gid)
        for _ in range(50):
            status = await engine.tell_status(gid)
            if status["status"] == "paused":
                break
            await asyncio.sleep(0.1)
        assert status["status"] == "paused", status.get("errorMessage")
        assert int(status.get("completedLength", "0")) < len(payload)
        await engine.unpause(gid)
        for _ in range(150):
            status = await engine.tell_status(gid)
            if status["status"] in {"complete", "error"}:
                break
            await asyncio.sleep(0.1)
        assert status["status"] == "complete", status.get("errorMessage")
        assert (task_dir / "slow.bin").read_bytes() == payload
    finally:
        await engine.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_real_magnet_metadata_and_torrent_rpc_options(tmp_path):
    if os.environ.get("GRABBIT_REAL_ARIA2_TEST") != "1" or not shutil.which("aria2c"):
        pytest.skip("requires GRABBIT_REAL_ARIA2_TEST=1 and aria2c")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    engine = Aria2Engine(state_dir=tmp_path / "private", download_root=downloads)
    blob = torrent()
    infohash = parse_torrent(blob).infohash
    try:
        await engine.start()
        metadata_gid = secrets.token_hex(8)
        await engine.add_magnet_metadata(f"magnet:?xt=urn:btih:{infohash}", downloads / "metadata", metadata_gid)
        assert (await engine.tell_status(metadata_gid))["status"] == "paused"
        await engine.unpause(metadata_gid)
        await asyncio.sleep(0.2)
        assert (await engine.tell_status(metadata_gid))["status"] in {"active", "waiting"}
        await engine.remove(metadata_gid)
        content_gid = secrets.token_hex(8)
        await engine.add_torrent(blob, downloads / "content", content_gid, [1])
        assert (await engine.tell_status(content_gid))["status"] == "paused"
        await engine.remove(content_gid)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_real_local_peer_magnet_selection_download(tmp_path):
    if os.environ.get("GRABBIT_REAL_ARIA2_TEST") != "1" or not shutil.which("aria2c"):
        pytest.skip("requires GRABBIT_REAL_ARIA2_TEST=1 and aria2c")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        peer_port = probe.getsockname()[1]
    peers = socket.inet_aton("127.0.0.1") + peer_port.to_bytes(2, "big")

    class Tracker(BaseHTTPRequestHandler):
        def do_GET(self):
            body = bencode({b"interval": 1, b"peers": peers})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    tracker = ThreadingHTTPServer(("127.0.0.1", 0), Tracker)
    thread = threading.Thread(target=tracker.serve_forever, daemon=True)
    thread.start()
    announce = f"http://127.0.0.1:{tracker.server_port}/announce".encode()
    payload = b"grabbit-local-peer-test" * 2048
    piece_length = 16384
    pieces = b"".join(hashlib.sha1(payload[start:start + piece_length]).digest() for start in range(0, len(payload), piece_length))
    blob = bencode({b"announce": announce, b"info": {b"length": len(payload), b"name": b"fixture.bin", b"piece length": piece_length, b"pieces": pieces}})
    infohash = parse_torrent(blob).infohash
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "fixture.bin").write_bytes(payload)
    torrent_file = tmp_path / "fixture.torrent"
    torrent_file.write_bytes(blob)
    seed_config = tmp_path / "seed.conf"
    seed_config.write_text("")
    seeder = await asyncio.create_subprocess_exec(
        "aria2c", f"--conf-path={seed_config}", f"--dir={seed_dir}",
        f"--listen-port={peer_port}", "--enable-dht=false", "--bt-enable-lpd=false",
        "--seed-time=2", "--check-integrity=true", "--file-allocation=none",
        str(torrent_file), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    engine = Aria2Engine(state_dir=tmp_path / "private", download_root=downloads)
    try:
        await asyncio.sleep(1)
        if seeder.returncode is not None:
            assert False, (await seeder.stderr.read()).decode(errors="replace")
        await engine.start()
        metadata_gid = secrets.token_hex(8)
        metadata_dir = downloads / "metadata"
        magnet = f"magnet:?xt=urn:btih:{infohash}&tr=http%3A%2F%2F127.0.0.1%3A{tracker.server_port}%2Fannounce"
        await engine.add_magnet_metadata(magnet, metadata_dir, metadata_gid)
        await engine.unpause(metadata_gid)
        for _ in range(200):
            status = await engine.tell_status(metadata_gid)
            if status["status"] in {"complete", "error"}:
                break
            await asyncio.sleep(0.1)
        assert status["status"] == "complete", status.get("errorMessage")
        saved = metadata_dir / f"{infohash}.torrent"
        assert saved.is_file()
        assert parse_torrent(saved.read_bytes()).infohash == infohash
        for followed in status.get("followedBy", []):
            await engine.remove(followed)
        content_gid = secrets.token_hex(8)
        content_dir = downloads / "content"
        await engine.add_torrent(saved.read_bytes(), content_dir, content_gid, [1])
        assert (await engine.tell_status(content_gid))["status"] == "paused"
        await engine.unpause(content_gid)
        for _ in range(200):
            status = await engine.tell_status(content_gid)
            if status["status"] in {"complete", "error"}:
                break
            await asyncio.sleep(0.1)
        assert status["status"] == "complete", status.get("errorMessage")
        assert (content_dir / "fixture.bin").read_bytes() == payload
        await asyncio.sleep(0.3)
        finished = await engine.tell_status(content_gid)
        assert finished["status"] == "complete"
        assert int(finished.get("uploadSpeed", "0")) == 0
    finally:
        await engine.close()
        if seeder.returncode is None:
            seeder.terminate()
            try:
                await asyncio.wait_for(seeder.wait(), 5)
            except asyncio.TimeoutError:
                seeder.kill()
                await seeder.wait()
        tracker.shutdown()
        tracker.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_real_magnet_http_api_queue_and_file_index(tmp_path, monkeypatch):
    if os.environ.get("GRABBIT_REAL_ARIA2_TEST") != "1" or not shutil.which("aria2c"):
        pytest.skip("requires GRABBIT_REAL_ARIA2_TEST=1 and aria2c")
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(User(username="admin", password_hash=hash_password("123456")))
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        await db.commit()
    root = tmp_path / "app-downloads"
    root.mkdir()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        download_root=root, private_root=tmp_path / "app-private",
        public_origin="http://testserver",
    )
    engine = Aria2Engine(state_dir=settings.private_root / "aria2", download_root=root)
    scheduler = scheduler_module.Scheduler(engine, settings)
    monkeypatch.setattr(task_api, "SessionLocal", sessions)
    monkeypatch.setattr(task_api, "get_settings", lambda: settings)
    monkeypatch.setattr(torrent_api, "SessionLocal", sessions)
    monkeypatch.setattr(torrent_api, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    async def override_session():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.scheduler = scheduler
    try:
        async with local_bt_peer(tmp_path) as (blob, payload, magnet):
            await engine.start()
            scheduler.available = True
            await scheduler.recover()
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
                login = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "123456"}, headers={"Origin": "http://testserver"})
                assert login.status_code == 200
                headers = {"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}
                created = await client.post("/api/v1/tasks/general", json={"sources": [magnet]}, headers=headers)
                assert created.status_code == 201, created.text
                task_id = created.json()["items"][0]["id"]
                state = None
                for _ in range(250):
                    await scheduler.tick()
                    response = await client.get(f"/api/v1/tasks/{task_id}")
                    state = response.json()["status"]
                    if state in {"awaiting_selection", "failed"}:
                        break
                    await asyncio.sleep(0.1)
                assert state == "awaiting_selection", response.text
                candidates = await client.get(f"/api/v1/tasks/{task_id}/files")
                assert candidates.status_code == 200
                assert [item["path"] for item in candidates.json()["items"]] == ["app-fixture.bin"]
                selected = await client.put(
                    f"/api/v1/tasks/{task_id}/selection",
                    json={"selected_indices": [1], "metadata_revision": candidates.json()["metadata_revision"]},
                    headers=headers,
                )
                assert selected.status_code == 200, selected.text
                for _ in range(250):
                    await scheduler.tick()
                    response = await client.get(f"/api/v1/tasks/{task_id}")
                    state = response.json()["status"]
                    if state in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.1)
                assert state == "completed", response.text
                async with sessions() as db:
                    indexed = (await db.scalars(select(FileRecord).where(FileRecord.task_id == task_id))).all()
                    assert [item.display_name for item in indexed] == ["app-fixture.bin"]
                files = await client.get(f"/api/v1/files?directory=general/{task_id}")
                assert files.status_code == 200
                assert [item["name"] for item in files.json()["items"]] == ["app-fixture.bin"]
                file_id = files.json()["items"][0]["id"]
                content = await client.get(f"/api/v1/files/{file_id}/content")
                assert content.status_code == 200
                assert content.content == payload
                history = await client.get("/api/v1/history")
                assert any(item["id"] == task_id for item in history.json()["items"])
                await engine.close()
                torrent_engine = Aria2Engine(state_dir=settings.private_root / "aria2-torrent", download_root=root)
                torrent_scheduler = scheduler_module.Scheduler(torrent_engine, settings)
                app.state.scheduler = torrent_scheduler
                try:
                    await torrent_engine.start()
                    torrent_scheduler.available = True
                    uploaded = await client.post(
                        "/api/v1/torrents",
                        files={"file": ("app-fixture.torrent", blob, "application/x-bittorrent")},
                        headers=headers,
                    )
                    assert uploaded.status_code == 201, uploaded.text
                    created_torrent = await client.post(
                        "/api/v1/tasks/torrent",
                        json={"torrent_id": uploaded.json()["torrent_id"], "selected_indices": [1], "allow_duplicates": True},
                        headers=headers,
                    )
                    assert created_torrent.status_code == 201, created_torrent.text
                    torrent_task_id = created_torrent.json()["id"]
                    for _ in range(250):
                        await torrent_scheduler.tick()
                        response = await client.get(f"/api/v1/tasks/{torrent_task_id}")
                        state = response.json()["status"]
                        if state in {"completed", "failed"}:
                            break
                        await asyncio.sleep(0.1)
                    assert state == "completed", response.text
                    async with sessions() as db:
                        indexed = (await db.scalars(select(FileRecord).where(FileRecord.task_id == torrent_task_id))).all()
                        assert [item.display_name for item in indexed] == ["app-fixture.bin"]
                    torrent_files = await client.get(f"/api/v1/files?directory=general/{torrent_task_id}")
                    assert [item["name"] for item in torrent_files.json()["items"]] == ["app-fixture.bin"]
                    torrent_content = await client.get(f"/api/v1/files/{torrent_files.json()['items'][0]['id']}/content")
                    assert torrent_content.content == payload
                finally:
                    await torrent_engine.close()
    finally:
        await engine.close()
        app.dependency_overrides.clear()
        await database.dispose()


@pytest.mark.parametrize("handler_type", [SlowHandler, SlowNoRangeHandler])
@pytest.mark.asyncio
async def test_real_engine_crash_recovers_http_download(tmp_path, monkeypatch, handler_type):
    if os.environ.get("GRABBIT_REAL_ARIA2_TEST") != "1" or not shutil.which("aria2c"):
        pytest.skip("requires GRABBIT_REAL_ARIA2_TEST=1 and aria2c")
    source = tmp_path / "source-crash"
    source.mkdir()
    payload = b"grabbit-crash-resume" * (1024 * 128)
    (source / "slow.bin").write_bytes(payload)
    handler = functools.partial(handler_type, directory=str(source))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crash.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "crash-downloads"
    root.mkdir()
    settings = Settings(download_root=root, private_root=tmp_path / "crash-private")
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        db.add(Task(id="crash-task", kind="general", source_type="http", source_url=f"http://127.0.0.1:{server.server_port}/slow.bin", source_fingerprint="crash", title="slow.bin", status="queued", download_subdir="general", task_directory_key="general/crash-task", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
        db.add(WorkItem(work_type="download", resource_id="crash-task", position=1))
        await db.commit()
    aria = Aria2Engine(state_dir=settings.private_root / "aria2", download_root=root)
    scheduler = scheduler_module.Scheduler(aria, settings)
    try:
        await aria.start()
        scheduler.available = True
        await scheduler.recover()
        await scheduler.tick()
        old_gid = scheduler.active["crash-task"]
        await asyncio.sleep(0.1)
        assert aria.process is not None
        aria.process.kill()
        await aria.process.wait()
        await scheduler.tick()
        assert scheduler.active["crash-task"] != old_gid
        state = None
        for _ in range(300):
            await scheduler.tick()
            async with sessions() as db:
                state = (await db.get(Task, "crash-task")).status
            if state in {"completed", "failed"}:
                break
            await asyncio.sleep(0.1)
        assert state == "completed"
        assert (root / "general" / "crash-task" / "slow.bin").read_bytes() == payload
    finally:
        await aria.close()
        await database.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_real_scheduler_downloads_two_http_tasks_serially(tmp_path, monkeypatch):
    if os.environ.get("GRABBIT_REAL_ARIA2_TEST") != "1" or not shutil.which("aria2c"):
        pytest.skip("requires GRABBIT_REAL_ARIA2_TEST=1 and aria2c")
    source = tmp_path / "source-serial"
    source.mkdir()
    payloads = {"first.bin": b"first" * (1024 * 256), "second.bin": b"second" * (1024 * 256)}
    for name, payload in payloads.items():
        (source / name).write_bytes(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(SlowHandler, directory=str(source)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'serial.db'}")
    sessions = async_sessionmaker(database, expire_on_commit=False)
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(scheduler_module, "SessionLocal", sessions)
    root = tmp_path / "serial-downloads"
    root.mkdir()
    settings = Settings(download_root=root, private_root=tmp_path / "serial-private")
    async with sessions() as db:
        db.add(Setting(key="disk_min_free_bytes", value_json="0", revision=1))
        for position, name in enumerate(payloads, 1):
            task_id = f"serial-{position}"
            db.add(Task(id=task_id, kind="general", source_type="http", source_url=f"http://127.0.0.1:{server.server_port}/{name}", source_fingerprint=task_id, title=name, status="queued", download_subdir="general", task_directory_key=f"general/{task_id}", progress_json="{}", selection_json="{}", options_json="{}", warnings_json="[]"))
            db.add(WorkItem(work_type="download", resource_id=task_id, position=position))
        await db.commit()
    aria = Aria2Engine(state_dir=settings.private_root / "aria2", download_root=root)
    scheduler = scheduler_module.Scheduler(aria, settings)
    try:
        await aria.start()
        scheduler.available = True
        await scheduler.recover()
        await scheduler.tick()
        assert set(scheduler.active) == {"serial-1"}
        async with sessions() as db:
            assert (await db.get(Task, "serial-2")).status == "queued"
            assert (await db.scalar(select(TaskAttempt.id).where(TaskAttempt.task_id == "serial-2"))) is None
        await scheduler.tick()
        assert set(scheduler.active) == {"serial-1"}
        states = None
        for _ in range(300):
            await scheduler.tick()
            assert len(scheduler.active) <= 1
            async with sessions() as db:
                states = [(await db.get(Task, task_id)).status for task_id in ("serial-1", "serial-2")]
            if states == ["completed", "completed"]:
                break
            await asyncio.sleep(0.1)
        assert states == ["completed", "completed"]
        for position, (name, payload) in enumerate(payloads.items(), 1):
            assert (root / "general" / f"serial-{position}" / name).read_bytes() == payload
    finally:
        await aria.close()
        await database.dispose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
