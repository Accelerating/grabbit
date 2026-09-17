from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.engines.aria2 import Aria2Engine, Aria2RpcError


class FakeProcess:
    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode: int | None = None

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_lifecycle_rpc_contract_and_private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []
    process = FakeProcess()

    async def fake_rpc(self: Aria2Engine, method: str, *, params=None, timeout=None):
        calls.append({"method": method, "params": [f"token:{self.rpc_secret}", *(params or [])]})
        if method == "aria2.getVersion":
            return {"version": "1.37.0", "enabledFeatures": ["BitTorrent"]}
        if method == "aria2.addUri":
            assert params[1]["pause"] == "true"
            assert params[1]["gid"] == "0123456789abcdef"
            return "0123456789abcdef"
        if method == "aria2.tellStatus":
            return {"gid": params[0], "status": "paused", "completedLength": "0"}
        if method == "aria2.getFiles":
            return [{"index": "1", "path": "file.bin", "selected": "true"}]
        if method in {"aria2.pause", "aria2.unpause", "aria2.remove"}:
            return params[0]
        if method == "aria2.shutdown":
            process.returncode = 0
        return "OK"

    monkeypatch.setattr(Aria2Engine, "_rpc", fake_rpc)
    executed: list[tuple[Any, ...]] = []

    async def fake_create(*args: Any, **kwargs: Any) -> FakeProcess:
        executed.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    root = tmp_path / "downloads"
    engine = Aria2Engine(state_dir=tmp_path / "private", download_root=root, rpc_port=6800)
    try:
        version = await engine.start()
        assert version["version"] == "1.37.0"
        assert executed == [("aria2c", f"--conf-path={engine.config_path}")]
        assert engine.config_path.stat().st_mode & 0o777 == 0o600
        config = engine.config_path.read_text()
        assert "enable-rpc=true" in config
        assert f"rpc-secret={engine.rpc_secret}" in config
        assert "rpc-listen-all=false" in config
        assert "rpc-max-request-size=16M" in config
        assert "seed-time=0" in config

        gid = await engine.add_uri("https://example.test/file.bin", root / "task", "0123456789abcdef")
        assert gid == "0123456789abcdef"
        assert (await engine.tell_status("0123456789abcdef"))["status"] == "paused"
        assert (await engine.get_files("0123456789abcdef"))[0]["index"] == "1"
        assert await engine.unpause("0123456789abcdef") == "0123456789abcdef"
        assert await engine.pause("0123456789abcdef") == "0123456789abcdef"
        assert await engine.remove("0123456789abcdef") == "0123456789abcdef"
        assert await engine.save_session() == "OK"
    finally:
        await engine.close()

    methods = [call["method"] for call in calls]
    assert methods[0] == "aria2.getVersion"
    assert "aria2.addUri" in methods
    assert methods[-2:] == ["aria2.saveSession", "aria2.shutdown"]
    assert all(call["params"][0] == f"token:{engine.rpc_secret}" for call in calls)


@pytest.mark.asyncio
async def test_directory_and_rpc_errors_are_safe(tmp_path: Path):
    engine = Aria2Engine(state_dir=tmp_path / "private", download_root=tmp_path / "downloads", rpc_port=6800)
    with pytest.raises(ValueError):
        await engine.add_uri("https://example.test/x", tmp_path / "outside", "0123456789abcdef")
    with pytest.raises(ValueError):
        await engine.tell_status("../../escape")
    with pytest.raises(ValueError):
        Aria2Engine(state_dir=tmp_path, rpc_host="0.0.0.0")

    # RPC errors preserve the code/message but never need to expose the secret.
    error = Aria2RpcError(1, "bad request")
    assert error.code == 1
    assert "bad request" in str(error)
    assert engine.rpc_secret not in str(error)
