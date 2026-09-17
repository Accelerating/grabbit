"""Safe, bounded parsing and private storage for uploaded BitTorrent files.

This module intentionally implements only metainfo ingestion.  It does not
hand untrusted bytes to aria2 or create a download task.  Parsing is strict
enough to reject malformed bencode and path entries before any later stage can
interpret them.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_BENCODE_DEPTH = 64
MAX_BENCODE_NODES = 100_000
MAX_TORRENT_FILES = 20_000
MAX_PATH_SEGMENTS = 128
MAX_NAME_BYTES = 1024


class TorrentParseError(ValueError):
    """The upload is not a safe, supported torrent metainfo document."""


@dataclass(frozen=True)
class _Node:
    value: object
    start: int
    end: int

    @property
    def raw(self) -> bytes:
        # Filled by the parser through its source buffer.  This property is
        # replaced on the parser-created subclass below; keeping nodes small
        # avoids copying every nested byte string.
        raise RuntimeError("raw is only available on parser nodes")


class _ParserNode(_Node):
    __slots__ = ("_source",)

    def __init__(self, value: object, start: int, end: int, source: bytes):
        super().__init__(value, start, end)
        object.__setattr__(self, "_source", source)

    @property
    def raw(self) -> bytes:
        return self._source[self.start:self.end]


class _BencodeParser:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.nodes = 0

    def parse(self) -> _ParserNode:
        if not self.data:
            raise TorrentParseError("Torrent is empty")
        node = self._parse(0)
        if self.pos != len(self.data):
            raise TorrentParseError("Trailing bytes after torrent metainfo")
        return node

    def _parse(self, depth: int) -> _ParserNode:
        if depth > MAX_BENCODE_DEPTH:
            raise TorrentParseError("Torrent nesting is too deep")
        self.nodes += 1
        if self.nodes > MAX_BENCODE_NODES or self.pos >= len(self.data):
            raise TorrentParseError("Torrent is too complex")
        start = self.pos
        marker = self.data[self.pos]
        if marker == ord("i"):
            self.pos += 1
            end = self.data.find(b"e", self.pos)
            if end < 0 or end - self.pos > 24:
                raise TorrentParseError("Invalid torrent integer")
            token = self.data[self.pos:end]
            if not token or token in {b"-0", b"00"} or (token.startswith(b"0") and len(token) > 1) or (token.startswith(b"-0") and len(token) > 2):
                raise TorrentParseError("Invalid torrent integer")
            try:
                value = int(token)
            except ValueError as exc:
                raise TorrentParseError("Invalid torrent integer") from exc
            self.pos = end + 1
            return _ParserNode(value, start, self.pos, self.data)
        if marker == ord("l"):
            self.pos += 1
            values: list[_ParserNode] = []
            while True:
                if self.pos >= len(self.data):
                    raise TorrentParseError("Unterminated torrent list")
                if self.data[self.pos] == ord("e"):
                    self.pos += 1
                    return _ParserNode(values, start, self.pos, self.data)
                values.append(self._parse(depth + 1))
        if marker == ord("d"):
            self.pos += 1
            values: dict[bytes, _ParserNode] = {}
            previous: bytes | None = None
            while True:
                if self.pos >= len(self.data):
                    raise TorrentParseError("Unterminated torrent dictionary")
                if self.data[self.pos] == ord("e"):
                    self.pos += 1
                    return _ParserNode(values, start, self.pos, self.data)
                key_node = self._parse(depth + 1)
                if not isinstance(key_node.value, bytes):
                    raise TorrentParseError("Torrent dictionary key is not a byte string")
                key = key_node.value
                # Duplicate keys are ambiguous and non-canonical ordering can
                # produce a different infohash in different implementations.
                if key in values or (previous is not None and key <= previous):
                    raise TorrentParseError("Torrent dictionary keys are not canonical")
                previous = key
                values[key] = self._parse(depth + 1)
        if 48 <= marker <= 57:
            colon = self.data.find(b":", self.pos)
            if colon < 0 or colon - self.pos > 12:
                raise TorrentParseError("Invalid torrent byte string")
            length_token = self.data[self.pos:colon]
            if not length_token or (length_token.startswith(b"0") and len(length_token) > 1):
                raise TorrentParseError("Invalid torrent byte string length")
            try:
                length = int(length_token)
            except ValueError as exc:
                raise TorrentParseError("Invalid torrent byte string length") from exc
            if length < 0 or length > len(self.data) or colon + 1 + length > len(self.data):
                raise TorrentParseError("Torrent byte string exceeds upload")
            self.pos = colon + 1
            end = self.pos + length
            value = self.data[self.pos:end]
            self.pos = end
            return _ParserNode(value, start, end, self.data)
        raise TorrentParseError("Invalid torrent bencode marker")


def _bytes(node: _ParserNode | None, field: str, *, max_len: int = MAX_NAME_BYTES) -> bytes:
    if node is None or not isinstance(node.value, bytes) or len(node.value) == 0 or len(node.value) > max_len:
        raise TorrentParseError(f"Torrent field {field!r} is invalid")
    return node.value


def _integer(node: _ParserNode | None, field: str, *, minimum: int = 0) -> int:
    if node is None or not isinstance(node.value, int) or node.value < minimum:
        raise TorrentParseError(f"Torrent field {field!r} is invalid")
    return node.value


def _text(value: bytes, field: str) -> str:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TorrentParseError(f"Torrent field {field!r} is not valid UTF-8") from exc
    if not text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise TorrentParseError(f"Torrent field {field!r} contains control characters")
    return text


def _path_segment(value: bytes, field: str) -> str:
    text = _text(value, field)
    if text in {".", ".."} or "/" in text or "\\" in text or text.startswith("~"):
        raise TorrentParseError(f"Unsafe torrent path component in {field!r}")
    return text


def _path(node: _ParserNode | None, field: str) -> list[str]:
    if node is None or not isinstance(node.value, list) or not node.value or len(node.value) > MAX_PATH_SEGMENTS:
        raise TorrentParseError(f"Torrent field {field!r} is invalid")
    return [_path_segment(item.value if isinstance(item, _ParserNode) else b"", field) for item in node.value]


@dataclass(frozen=True)
class ParsedTorrent:
    blob: bytes
    infohash: str
    summary: dict


def parse_torrent(blob: bytes) -> ParsedTorrent:
    """Parse one bounded torrent blob and return a JSON-safe summary."""
    if len(blob) > MAX_UPLOAD_BYTES:
        raise TorrentParseError("Torrent upload exceeds the 10 MiB limit")
    root = _BencodeParser(blob).parse()
    if not isinstance(root.value, dict):
        raise TorrentParseError("Torrent top level must be a dictionary")
    info = root.value.get(b"info")
    if info is None or not isinstance(info.value, dict):
        raise TorrentParseError("Torrent info dictionary is missing")
    info_dict: dict[bytes, _ParserNode] = info.value
    name = _path_segment(_bytes(info_dict.get(b"name"), "name"), "name")
    piece_length = _integer(info_dict.get(b"piece length"), "piece length", minimum=1)
    pieces = _bytes(info_dict.get(b"pieces"), "pieces", max_len=MAX_UPLOAD_BYTES)
    if len(pieces) == 0 or len(pieces) % 20:
        raise TorrentParseError("Torrent pieces field must contain complete SHA-1 hashes")
    has_length = b"length" in info_dict
    has_files = b"files" in info_dict
    if has_length == has_files:
        raise TorrentParseError("Torrent must contain exactly one of length or files")

    files: list[dict[str, int | str]] = []
    if has_length:
        length = _integer(info_dict.get(b"length"), "length")
        files.append({"index": 1, "path": name, "size_bytes": length})
    else:
        files_node = info_dict.get(b"files")
        if files_node is None or not isinstance(files_node.value, list) or not files_node.value:
            raise TorrentParseError("Torrent files field is invalid")
        if len(files_node.value) > MAX_TORRENT_FILES:
            raise TorrentParseError("Torrent contains too many files")
        seen_paths: set[str] = set()
        for index, file_node in enumerate(files_node.value, 1):
            if not isinstance(file_node.value, dict):
                raise TorrentParseError("Torrent file entry is invalid")
            file_dict: dict[bytes, _ParserNode] = file_node.value
            path = [name, *_path(file_dict.get(b"path"), "path")]
            joined = "/".join(path)
            if joined in seen_paths:
                raise TorrentParseError("Torrent contains duplicate file paths")
            seen_paths.add(joined)
            attr = file_dict.get(b"attr")
            if attr is not None and isinstance(attr.value, bytes) and b"l" in attr.value:
                raise TorrentParseError("Torrent symlink entries are not supported")
            files.append({"index": index, "path": joined, "size_bytes": _integer(file_dict.get(b"length"), "file length")})

    total_size = sum(int(item["size_bytes"]) for item in files)
    if total_size > 2**63 - 1:
        raise TorrentParseError("Torrent total size is too large")
    expected_pieces = (total_size + piece_length - 1) // piece_length
    if len(pieces) // 20 != expected_pieces:
        raise TorrentParseError("Torrent piece hashes do not match total file size")
    infohash = hashlib.sha1(info.raw).hexdigest()
    summary = {
        "name": name,
        "infohash": infohash,
        "piece_length": piece_length,
        "file_count": len(files),
        "total_size_bytes": total_size,
        "files": files,
    }
    announce = root.value.get(b"announce")
    if announce is not None and isinstance(announce.value, bytes):
        # Announce is informational only; never use it as a URL without a
        # separate scheme/host validation at task creation time.
        try:
            summary["announce"] = announce.value.decode("utf-8")
        except UnicodeDecodeError:
            summary["announce"] = None
    return ParsedTorrent(blob=blob, infohash=infohash, summary=summary)


def torrent_blob_path(private_root: Path, blob_key: str) -> Path:
    """Resolve a previously generated key without accepting path traversal."""
    if not blob_key.startswith("torrents/") or Path(blob_key).name != blob_key.split("/", 1)[1] or Path(blob_key).name != f"{Path(blob_key).stem}.torrent":
        raise ValueError("invalid torrent blob key")
    return private_root / blob_key


def persist_torrent_blob(private_root: Path, blob: bytes, torrent_id: str | None = None) -> str:
    """Atomically persist bytes with mode 0600 under the private root."""
    torrent_id = torrent_id or str(uuid4())
    directory = private_root / "torrents"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    blob_key = f"torrents/{torrent_id}.torrent"
    destination = torrent_blob_path(private_root, blob_key)
    fd, temp_name = tempfile.mkstemp(prefix=f".{torrent_id}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return blob_key


def summary_json(summary: dict) -> str:
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
