from __future__ import annotations

import socket

import pytest

from app.engines import dht


@pytest.mark.asyncio
async def test_cold_table_seeding_is_readable_by_aria2_and_reused(tmp_path, monkeypatch):
    calls = []
    remote_id = b"a" * 20
    ip = socket.inet_aton("8.8.8.8")

    async def query(host, port, node_id):
        calls.append(host)
        return [(remote_id, ip, 6881)]

    monkeypatch.setattr(dht, "_query", query)
    path = tmp_path / "dht.dat"
    await dht.seed_routing_table(path)
    data = path.read_bytes()
    assert dht.HEADER.size == dht.NODE.size == 56
    header = dht.HEADER.unpack(data[:56])
    assert header[:3] == (b"\xa1\xa2", 2, 3)
    assert header[-1] == 1
    assert dht.NODE.unpack(data[56:]) == (6, ip, 6881, remote_id)
    assert path.stat().st_mode & 0o777 == 0o600
    await dht.seed_routing_table(path)
    assert len(calls) == len(dht.BOOTSTRAPS)
    assert path.read_bytes() == data


@pytest.mark.asyncio
async def test_empty_table_reseeded_and_network_failure_is_nonfatal(tmp_path, monkeypatch):
    async def unavailable(*args):
        return []

    monkeypatch.setattr(dht, "_query", unavailable)
    path = tmp_path / "dht.dat"
    await dht.seed_routing_table(path)
    assert not path.exists()
    path.write_bytes(dht.HEADER.pack(b"\xa1\xa2", 2, 3, 1, 0, b"a" * 20, 0))
    async def available(*args):
        return [(b"b" * 20, socket.inet_aton("8.8.4.4"), 6881)]
    monkeypatch.setattr(dht, "_query", available)
    await dht.seed_routing_table(path)
    assert int.from_bytes(path.read_bytes()[48:52], "big") == 1


def test_dht_decoder_rejects_malformed_packets():
    assert dht._decode(b"d1:rd5:nodes0:e1:t2:gg1:y1:re")[b"r"][b"nodes"] == b""
    for packet in [b"", b"d", b"d1:rd5:nodes999:abc", b"d1:t2:gg1:t2:gge", b"degarbage"]:
        with pytest.raises(ValueError):
            dht._decode(packet)
