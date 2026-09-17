"""Lifecycle and JSON-RPC adapter for the application-owned aria2 process.

The adapter deliberately uses only the Python standard library for its RPC
transport.  aria2 is an implementation detail of the queue and is always
started by this process with a private configuration file; callers never
provide command-line arguments or an RPC endpoint directly.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import signal
import socket
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class Aria2Error(RuntimeError):
    """Base error raised by :class:`Aria2Engine`."""


class Aria2ProcessError(Aria2Error):
    """The managed aria2 process could not be started or stopped."""


class Aria2RpcError(Aria2Error):
    """An aria2 JSON-RPC call returned an error."""

    def __init__(self, code: int | None, message: str):
        self.code = code
        self.message = message
        suffix = f" ({code})" if code is not None else ""
        super().__init__(f"aria2 RPC error{suffix}: {message}")


class Aria2Engine:
    """Own one local aria2c process and expose the small API used by the queue.

    ``download_root`` is optional to keep this class useful for recovery and
    tests.  When supplied, ``add_uri`` rejects directories outside that root,
    including symlink escapes.  The scheduler should normally pass a task
    directory below the configured root.

    The return types intentionally mirror aria2: ``add_uri`` and control calls
    return the engine's GID/result string, status calls return decoded JSON
    mappings, and ``get_files`` returns the decoded file list.
    """

    _LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
    # aria2's manually supplied GID must be exactly 16 hex characters;
    # all-zero is reserved by aria2.
    _GID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")

    def __init__(
        self,
        *,
        state_dir: Path,
        download_root: Path | None = None,
        executable: str = "aria2c",
        rpc_host: str = "127.0.0.1",
        rpc_port: int = 0,
        rpc_secret: str | None = None,
        startup_timeout: float = 10.0,
        rpc_timeout: float = 10.0,
        max_rpc_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if rpc_host not in self._LOCAL_HOSTS:
            raise ValueError("aria2 RPC must listen on localhost")
        if rpc_port < 0 or rpc_port > 65535 or (rpc_port != 0 and rpc_port < 1024):
            raise ValueError("rpc_port must be 0 or between 1024 and 65535")
        if startup_timeout <= 0 or rpc_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if max_rpc_response_bytes <= 0:
            raise ValueError("max_rpc_response_bytes must be positive")
        if not executable or "\x00" in executable:
            raise ValueError("executable must be a non-empty path")

        self.state_dir = Path(state_dir).expanduser().resolve()
        self.download_root = Path(download_root).expanduser().resolve() if download_root else None
        self.executable = executable
        self.rpc_host = rpc_host
        self.rpc_port = rpc_port
        self.rpc_secret = rpc_secret or secrets.token_urlsafe(32)
        if not self.rpc_secret or any(char in self.rpc_secret for char in "\r\n\x00"):
            raise ValueError("rpc_secret must not contain control characters")
        self.startup_timeout = startup_timeout
        self.rpc_timeout = rpc_timeout
        self.max_rpc_response_bytes = max_rpc_response_bytes

        self._process: asyncio.subprocess.Process | None = None
        self._version: dict[str, Any] | None = None
        self._request_id = 0
        self._lifecycle_lock = asyncio.Lock()
        self._rpc_lock = asyncio.Lock()
        self._config_path = self.state_dir / "aria2.conf"
        self._session_path = self.state_dir / "aria2.session"

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        """The managed process, primarily for diagnostics and tests."""

        return self._process

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def session_path(self) -> Path:
        return self._session_path

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> dict[str, Any]:
        """Start aria2c and verify its RPC with ``getVersion``.

        The returned mapping is the actual aria2 ``getVersion`` result.  A
        second call is idempotent while the original process is alive.
        """

        async with self._lifecycle_lock:
            if self.is_running and self._version is not None:
                return dict(self._version)
            if self._process is not None:
                await self._terminate_process()

            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.state_dir.chmod(0o700)
            if self.rpc_port == 0:
                self.rpc_port = self._find_free_port()
            self._write_config()

            args = [self.executable, f"--conf-path={self._config_path}"]
            # Never inherit ambient proxy credentials or routing. The service
            # owns network policy; a shell proxy can silently redirect even a
            # localhost test source and leak signed download URLs.
            child_env = {key: value for key, value in os.environ.items() if key.lower() not in {
                "http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy",
            }}
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *args,
                    env=child_env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
            except (OSError, ValueError) as exc:
                self._process = None
                raise Aria2ProcessError(f"unable to start aria2c: {exc}") from exc

            deadline = asyncio.get_running_loop().time() + self.startup_timeout
            while True:
                if self._process.returncode is not None:
                    code = self._process.returncode
                    await self._terminate_process()
                    raise Aria2ProcessError(f"aria2c exited during startup (code {code})")
                try:
                    version = await self._rpc("aria2.getVersion", timeout=min(self.rpc_timeout, 1.0))
                    if not isinstance(version, Mapping) or not isinstance(version.get("version"), str):
                        raise Aria2ProcessError("aria2 getVersion returned an invalid response")
                    self._version = dict(version)
                    return dict(self._version)
                except Aria2RpcError:
                    await self._terminate_process()
                    raise
                except (Aria2Error, OSError, asyncio.TimeoutError, TimeoutError):
                    if asyncio.get_running_loop().time() >= deadline:
                        await self._terminate_process()
                        raise Aria2ProcessError("timed out waiting for aria2 RPC")
                    await asyncio.sleep(0.05)

    async def close(self) -> None:
        """Save the session and stop aria2; safe to call more than once."""

        async with self._lifecycle_lock:
            process = self._process
            if process is None:
                return
            first_error: BaseException | None = None
            if process.returncode is None:
                try:
                    await self.save_session()
                except BaseException as exc:  # shutdown must still be attempted
                    first_error = exc
                try:
                    await self._rpc("aria2.shutdown", timeout=min(self.rpc_timeout, 2.0))
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, OSError):
                await self._terminate_process()
            finally:
                self._process = None
                self._version = None
            if first_error is not None and not isinstance(first_error, Aria2RpcError):
                raise first_error

    async def save_session(self) -> str:
        result = await self._rpc("aria2.saveSession")
        return self._result_string(result)

    async def shutdown(self) -> str:
        """Ask aria2 to exit gracefully without releasing the process handle."""

        return self._result_string(await self._rpc("aria2.shutdown"))

    async def add_uri(self, uri: str, directory: str | Path, gid: str) -> str:
        """Add one URI paused, with a scheduler-chosen GID and task directory."""

        if not isinstance(uri, str) or not uri.strip() or any(char in uri for char in "\r\n\x00"):
            raise ValueError("uri must be a non-empty value without control characters")
        self._validate_gid(gid)
        target = self._safe_directory(directory)
        target.mkdir(parents=True, exist_ok=True)
        result = await self._rpc(
            "aria2.addUri",
            params=[[uri], {"dir": str(target), "gid": gid, "pause": "true"}],
        )
        return self._result_string(result)

    async def add_torrent(self, blob: bytes, directory: str | Path, gid: str, selected_indices: list[int]) -> str:
        """Add a validated uploaded torrent paused, selecting only aria2 file indices."""
        if not isinstance(blob, bytes) or not blob or len(blob) > 10 * 1024 * 1024:
            raise ValueError("torrent data must be 1–10 MiB")
        self._validate_gid(gid)
        if not selected_indices or len(selected_indices) > 20_000 or any(not isinstance(i, int) or i < 1 for i in selected_indices):
            raise ValueError("selected file indices are invalid")
        if len(set(selected_indices)) != len(selected_indices):
            raise ValueError("selected file indices contain duplicates")
        target = self._safe_directory(directory)
        target.mkdir(parents=True, exist_ok=True)
        encoded = base64.b64encode(blob).decode("ascii")
        result = await self._rpc(
            "aria2.addTorrent",
            params=[encoded, [], {
                "dir": str(target), "gid": gid, "pause": "true",
                "select-file": ",".join(str(index) for index in sorted(selected_indices)),
                "seed-time": "0",
            }],
        )
        return self._result_string(result)

    async def add_magnet_metadata(self, uri: str, directory: str | Path, gid: str) -> str:
        """Fetch magnet metainfo without starting its content download."""
        if not uri.startswith("magnet:?") or len(uri) > 8192:
            raise ValueError("invalid magnet URI")
        self._validate_gid(gid)
        target = self._safe_directory(directory)
        target.mkdir(parents=True, exist_ok=True)
        result = await self._rpc(
            "aria2.addUri",
            params=[[uri], {
                "dir": str(target), "gid": gid, "pause": "true",
                "bt-metadata-only": "true", "bt-save-metadata": "true",
                "pause-metadata": "true",
            }],
        )
        return self._result_string(result)

    async def tell_status(self, gid: str) -> dict[str, Any]:
        self._validate_gid(gid)
        result = await self._rpc(
            "aria2.tellStatus",
            params=[gid, [
                "gid", "status", "totalLength", "completedLength", "downloadSpeed",
                "uploadSpeed", "connections", "numSeeders", "errorCode",
                "errorMessage", "dir", "infoHash", "followedBy", "follows", "belongsTo",
            ]],
        )
        if not isinstance(result, Mapping):
            raise Aria2Error("aria2 tellStatus returned an invalid response")
        return dict(result)

    async def get_files(self, gid: str) -> list[dict[str, Any]]:
        self._validate_gid(gid)
        result = await self._rpc("aria2.getFiles", params=[gid])
        if not isinstance(result, list) or not all(isinstance(item, Mapping) for item in result):
            raise Aria2Error("aria2 getFiles returned an invalid response")
        return [dict(item) for item in result]

    async def unpause(self, gid: str) -> str:
        self._validate_gid(gid)
        return self._result_string(await self._rpc("aria2.unpause", params=[gid]))

    async def pause(self, gid: str) -> str:
        self._validate_gid(gid)
        return self._result_string(await self._rpc("aria2.pause", params=[gid]))

    async def remove(self, gid: str) -> str:
        self._validate_gid(gid)
        return self._result_string(await self._rpc("aria2.remove", params=[gid]))

    async def get_version(self) -> dict[str, Any]:
        result = await self._rpc("aria2.getVersion")
        if not isinstance(result, Mapping):
            raise Aria2Error("aria2 getVersion returned an invalid response")
        return dict(result)

    async def rpc(
        self,
        method: str,
        params: Sequence[Any] | None = None,
    ) -> Any:
        """Call a whitelisted-style aria2 method for future adapter additions."""

        if not isinstance(method, str) or not method or "\x00" in method:
            raise ValueError("method must be a non-empty string")
        normalized = method if "." in method else f"aria2.{method}"
        if not normalized.startswith(("aria2.", "system.")):
            raise ValueError("only aria2/system RPC methods are supported")
        return await self._rpc(normalized, params=params)

    async def _rpc(
        self,
        method: str,
        *,
        params: Sequence[Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if not self.is_running:
            raise Aria2ProcessError("aria2 process is not running")
        self._request_id += 1
        # aria2's RPC authentication convention includes the literal token:
        # prefix.  The bare secret is not accepted by aria2.
        call_params = [f"token:{self.rpc_secret}"]
        if params:
            call_params.extend(params)
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": call_params,
        }
        # aria2 handles concurrent RPC, but serializing calls makes lifecycle
        # shutdown and the single process's request ordering deterministic.
        async with self._rpc_lock:
            response = await self._http_request(payload, timeout=timeout or self.rpc_timeout)
        if not isinstance(response, Mapping):
            raise Aria2Error("aria2 returned a non-object JSON-RPC response")
        if response.get("id") != payload["id"]:
            raise Aria2Error("aria2 returned a mismatched JSON-RPC id")
        if "error" in response:
            error = response.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
                message = str(error.get("message", "unknown error"))
                raise Aria2RpcError(code if isinstance(code, int) else None, message)
            raise Aria2RpcError(None, "unknown error")
        if "result" not in response:
            raise Aria2Error("aria2 JSON-RPC response had neither result nor error")
        return response["result"]

    async def _http_request(self, payload: Mapping[str, Any], *, timeout: float) -> Any:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = (
            b"POST /jsonrpc HTTP/1.1\r\n"
            + f"Host: {self.rpc_host}:{self.rpc_port}\r\n".encode("ascii")
            + b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        try:
            async with asyncio.timeout(timeout):
                reader, writer = await asyncio.open_connection(self.rpc_host, self.rpc_port)
                try:
                    writer.write(request)
                    await writer.drain()
                    raw_headers = await reader.readuntil(b"\r\n\r\n")
                    status, headers = self._parse_headers(raw_headers)
                    if status != 200:
                        raise Aria2Error(f"aria2 RPC HTTP status {status}")
                    content_length = headers.get("content-length")
                    if content_length is not None:
                        content_length_int = int(content_length)
                        if content_length_int < 0 or content_length_int > self.max_rpc_response_bytes:
                            raise Aria2Error("aria2 RPC response is too large")
                        response_body = await reader.readexactly(content_length_int)
                    elif headers.get("transfer-encoding", "").lower() == "chunked":
                        response_body = await self._read_chunked(reader, self.max_rpc_response_bytes)
                    else:
                        response_body = await reader.read(self.max_rpc_response_bytes + 1)
                        if len(response_body) > self.max_rpc_response_bytes:
                            raise Aria2Error("aria2 RPC response is too large")
                finally:
                    writer.close()
                    with contextlib.suppress(OSError):
                        await writer.wait_closed()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, json.JSONDecodeError) as exc:
            raise Aria2Error("invalid response from aria2 RPC") from exc
        return json.loads(response_body)

    @staticmethod
    def _parse_headers(raw_headers: bytes) -> tuple[int, dict[str, str]]:
        lines = raw_headers.decode("iso-8859-1").split("\r\n")
        if not lines or len(lines[0].split()) < 2:
            raise Aria2Error("invalid aria2 RPC HTTP response")
        try:
            status = int(lines[0].split()[1])
        except ValueError as exc:
            raise Aria2Error("invalid aria2 RPC HTTP status") from exc
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator:
                raise Aria2Error("invalid aria2 RPC HTTP header")
            headers[name.strip().lower()] = value.strip()
        return status, headers

    @staticmethod
    async def _read_chunked(reader: asyncio.StreamReader, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size_text = size_line[:-2].split(b";", 1)[0].strip()
            size = int(size_text, 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                return b"".join(chunks)
            total += size
            if total > max_bytes:
                raise Aria2Error("aria2 RPC response is too large")
            chunks.append(await reader.readexactly(size))
            await reader.readexactly(2)

    def _write_config(self) -> None:
        # Keep all operational settings in a 0600 file so the RPC secret never
        # appears in the process list.  --conf-path itself is safe to expose.
        options = {
            "enable-rpc": "true",
            "rpc-listen-all": "false",
            "rpc-listen-port": str(self.rpc_port),
            "rpc-secret": self.rpc_secret,
            "rpc-allow-origin-all": "false",
            "rpc-max-request-size": "16M",
            "dir": str(self.download_root or self.state_dir),
            "save-session": str(self._session_path),
            "save-session-interval": "30",
            "seed-time": "0",
            "file-allocation": "none",
            "disk-cache": "8M",
            "max-connection-per-server": "2",
            "split": "2",
            "bt-max-peers": "20",
            "summary-interval": "1",
            "max-tries": "3",
            "retry-wait": "5",
            "continue": "true",
            "console-log-level": "warn",
            "log-level": "warn",
        }
        content = "".join(f"{key}={value}\n" for key, value in options.items())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".aria2.", dir=self.state_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_name, self._config_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
        self._session_path.touch(mode=0o600, exist_ok=True)
        self._session_path.chmod(0o600)

    def _safe_directory(self, directory: str | Path) -> Path:
        target = Path(directory).expanduser().resolve(strict=False)
        if self.download_root is not None:
            try:
                target.relative_to(self.download_root)
            except ValueError as exc:
                raise ValueError("download directory is outside the configured root") from exc
        return target

    @classmethod
    def _validate_gid(cls, gid: str) -> None:
        if not isinstance(gid, str) or not cls._GID_RE.fullmatch(gid) or int(gid, 16) == 0:
            raise ValueError("gid must be a non-zero 16-character hexadecimal value")

    @staticmethod
    def _result_string(result: Any) -> str:
        if not isinstance(result, str):
            raise Aria2Error("aria2 returned an invalid result")
        return result

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        self._process = None
        self._version = None
