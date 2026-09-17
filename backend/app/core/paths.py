from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.core.errors import ApiError


def safe_subdirectory(root: Path, raw: str) -> Path:
    if not raw or "\x00" in raw:
        raise ApiError(422, "PATH_INVALID", "Download subdirectory is invalid")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ApiError(422, "PATH_INVALID", "Download subdirectory is invalid")
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ApiError(422, "PATH_INVALID", "Symbolic links are not allowed")
    if not candidate.resolve(strict=False).is_relative_to(root.resolve()):
        raise ApiError(422, "PATH_INVALID", "Path escapes the download root")
    return candidate
