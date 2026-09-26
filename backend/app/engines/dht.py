"""Bounded cold-start seeding of aria2's IPv4 DHT routing table."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from pathlib import Path
import secrets
import socket
import struct
import tempfile
import time

logger = logging.getLogger(__name__)
BOOTSTRAPS = (("dht.transmissionbt.com", 6881), ("dht.libtorrent.org", 25401))
HEADER = struct.Struct("!2sB3xHQQ20s4xI4x")
NODE = struct.Struct("!B7x4sH18x20s4x")


def _decode(data: bytes) -> dict:
    """Decode a small DHT packet, rejecting truncation and excessive nesting."""
    pos = 0

    def value(depth=0):
        nonlocal pos
        if depth > 8 or pos >= len(data):
            raise ValueError("invalid DHT packet")
        marker = data[pos:pos + 1]
        if marker in (b"d", b"l"):
            pos += 1
            result = {} if marker == b"d" else []
            while pos < len(data) and data[pos:pos + 1] != b"e":
                item = value(depth + 1)
                if marker == b"d":
                    if not isinstance(item, bytes) or item in result:
                        raise ValueError("invalid DHT key")
                    result[item] = value(depth + 1)
                else:
                    result.append(item)
            if pos >= len(data):
                raise ValueError("truncated DHT packet")
            pos += 1
            return result
        if marker == b"i":
            end = data.index(b"e", pos)
            result = int(data[pos + 1:end])
            pos = end + 1
            return result
        end = data.index(b":", pos)
        size = int(data[pos:end])
        pos = end + 1
        if size < 0 or pos + size > len(data):
            raise ValueError("invalid DHT string")
        result = data[pos:pos + size]
        pos += size
        return result

    result = value()
    if pos != len(data) or not isinstance(result, dict):
        raise ValueError("invalid DHT response")
    return result


def _fetch(address: str, port: int, node_id: bytes) -> list[tuple[bytes, bytes, int]]:
    tx = secrets.token_bytes(4)
    packet = (b"d1:ad2:id20:" + node_id + b"6:target20:" + node_id
              + b"e1:q9:find_node1:t4:" + tx + b"1:y1:qe")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.connect((address, port))
        sock.send(packet)
        response = _decode(sock.recv(4096))
    if response.get(b"t") != tx or response.get(b"y") != b"r":
        raise ValueError("unexpected DHT response")
    reply = response.get(b"r")
    nodes = reply.get(b"nodes") if isinstance(reply, dict) else None
    if not isinstance(nodes, bytes) or len(nodes) % 26:
        raise ValueError("invalid compact DHT nodes")
    result = []
    for offset in range(0, len(nodes), 26):
        item = nodes[offset:offset + 26]
        remote_id, ip, remote_port = item[:20], item[20:24], int.from_bytes(item[24:], "big")
        if ipaddress.IPv4Address(ip).is_global and remote_port and remote_id != node_id:
            result.append((remote_id, ip, remote_port))
    return result


async def _query(host: str, port: int, node_id: bytes):
    async def query():
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM,
        )
        return await asyncio.to_thread(_fetch, addresses[0][4][0], port, node_id)
    try:
        return await asyncio.wait_for(query(), timeout=4)
    except (OSError, ValueError, TypeError, IndexError, TimeoutError):
        return []


async def seed_routing_table(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("DHT routing table must not be a symlink")
    if path.exists() and path.stat().st_size <= HEADER.size + 1000 * NODE.size:
        data = path.read_bytes()
        if len(data) >= HEADER.size and data[:8] == b"\xa1\xa2\x02\x00\x00\x00\x00\x03":
            count = int.from_bytes(data[48:52], "big")
            if len(data) == HEADER.size + count * NODE.size:
                public_nodes = sum(
                    ipaddress.IPv4Address(NODE.unpack(data[offset:offset + NODE.size])[1]).is_global
                    for offset in range(HEADER.size, len(data), NODE.size)
                )
                # A bootstrap-only or nearly empty table is not a successful
                # bootstrap. Retry seeding rather than retaining that state.
                if public_nodes >= 8:
                    return
    node_id = secrets.token_bytes(20)
    replies = await asyncio.gather(*(_query(host, port, node_id) for host, port in BOOTSTRAPS))
    nodes = list(dict.fromkeys(node for reply in replies for node in reply))[:64]
    if not nodes:
        logger.warning("DHT cold-start bootstrap unavailable; aria2 will use its configured entry point")
        return
    data = HEADER.pack(b"\xa1\xa2", 2, 3, int(time.time()), 0, node_id, len(nodes))
    data += b"".join(NODE.pack(6, ip, port, remote_id) for remote_id, ip, port in nodes)
    fd, name = tempfile.mkstemp(prefix=".dht-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    logger.info("Seeded DHT routing table with %d nodes", len(nodes))
