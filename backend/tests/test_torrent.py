from __future__ import annotations

import os

import pytest

from app.services.torrent import (
    MAX_UPLOAD_BYTES,
    TorrentParseError,
    parse_torrent,
    persist_torrent_blob,
    torrent_blob_path,
)


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(key) + bencode(item) for key, item in sorted(value.items())) + b"e"
    raise TypeError(value)


def torrent(*, path=b"video.mkv", length=12):
    info = {
        b"length": length,
        b"name": path,
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
    return bencode({b"announce": b"http://tracker.invalid/announce", b"info": info})


def test_parse_single_file_returns_infohash_and_candidates():
    parsed = parse_torrent(torrent())
    assert len(parsed.infohash) == 40
    assert parsed.summary["file_count"] == 1
    assert parsed.summary["total_size_bytes"] == 12
    assert parsed.summary["files"] == [{"index": 1, "path": "video.mkv", "size_bytes": 12}]


def test_parse_rejects_traversal_duplicate_and_symlink_paths():
    base = {b"name": b"root", b"piece length": 16384, b"pieces": b"x" * 20}
    with pytest.raises(TorrentParseError):
        parse_torrent(bencode({b"info": {**base, b"files": [{b"length": 1, b"path": [b"..", b"out"]}]}}))
    with pytest.raises(TorrentParseError):
        parse_torrent(bencode({b"info": {**base, b"files": [{b"length": 1, b"path": [b"a"]}, {b"length": 1, b"path": [b"a"]}]}}))
    with pytest.raises(TorrentParseError):
        parse_torrent(bencode({b"info": {**base, b"files": [{b"attr": b"l", b"length": 1, b"path": [b"a"]}]}}))


def test_upload_is_bounded_and_private(tmp_path):
    with pytest.raises(TorrentParseError):
        parse_torrent(b"x" * (MAX_UPLOAD_BYTES + 1))
    blob = torrent()
    key = persist_torrent_blob(tmp_path, blob, "00000000-0000-0000-0000-000000000001")
    path = torrent_blob_path(tmp_path, key)
    assert path.read_bytes() == blob
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
