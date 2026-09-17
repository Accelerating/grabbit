from __future__ import annotations

from typing import Any, Protocol


class DownloadEngine(Protocol):
    """One process-owned attempt; adapters must never own queue dispatch."""

    async def start(self, task_id: str, attempt_id: str, options: dict[str, Any]) -> str:
        """Start an attempt and return a durable engine reference."""

    async def inspect(self, engine_ref: str) -> dict[str, Any]:
        """Return a normalized, secret-free status snapshot."""

    async def stop(self, engine_ref: str) -> None:
        """Request stop and await engine confirmation before releasing a slot."""
