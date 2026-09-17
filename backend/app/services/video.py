"""Bounded yt-dlp metadata extraction; never expose raw extractor output."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path
from urllib.parse import urlsplit

from app.core.errors import ApiError

MAX_OUTPUT = 4 * 1024 * 1024


class OutputTooLarge(Exception):
    pass


async def _bounded_stdout(stream: asyncio.StreamReader) -> bytes:
    chunks = bytearray()
    while chunk := await stream.read(65536):
        if len(chunks) + len(chunk) > MAX_OUTPUT:
            raise OutputTooLarge
        chunks.extend(chunk)
    return bytes(chunks)


def video_site(url: str) -> str:
    if len(url) > 8192 or any(ord(ch) < 32 for ch in url):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid video URL")
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ApiError(422, "VALIDATION_ERROR", "Invalid video URL")
    host = parts.hostname.lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}:
        return "youtube"
    if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}:
        return "bilibili"
    raise ApiError(422, "UNSUPPORTED_SITE", "Only YouTube and Bilibili are supported for video analysis")


def summarize_video(info: dict, site: str) -> dict:
    if info.get("_type") in {"playlist", "multi_video"}:
        raise ApiError(422, "COLLECTION_NOT_SUPPORTED", "Collection analysis is not available yet")
    formats = []
    for item in info.get("formats") or []:
        if not isinstance(item, dict) or not isinstance(item.get("format_id"), str):
            continue
        formats.append({
            "id": item["format_id"][:128], "ext": str(item.get("ext") or "")[:16],
            "height": item.get("height") if isinstance(item.get("height"), int) else None,
            "vcodec": str(item.get("vcodec") or "none")[:64],
            "acodec": str(item.get("acodec") or "none")[:64],
            "filesize": item.get("filesize") if isinstance(item.get("filesize"), int) else None,
        })
        if len(formats) >= 200:
            break
    languages = sorted({str(key)[:32] for field in ("subtitles", "automatic_captions") for key in (info.get(field) or {}) if isinstance(key, str) and "live_chat" not in key.lower()})[:100]
    return {
        "site": site, "title": str(info.get("title") or "Untitled")[:255],
        "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
        # Extractor thumbnail URLs can carry signed query tokens. A private
        # authenticated thumbnail cache is needed before exposing them.
        "thumbnail": None,
        "extractor": str(info.get("extractor_key") or "")[:64],
        "formats": formats, "subtitle_languages": languages,
    }


def _collection_entry_url(item: dict, site: str, parent_url: str, ordinal: int) -> str | None:
    for key in ("webpage_url", "url"):
        candidate = item.get(key)
        if isinstance(candidate, str) and candidate.startswith(("https://", "http://")):
            try:
                if video_site(candidate) == site:
                    return candidate[:8192]
            except ApiError:
                pass
    media_id = item.get("id")
    if isinstance(media_id, str):
        if site == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{6,32}", media_id):
            return f"https://www.youtube.com/watch?v={media_id}"
        if site == "bilibili" and re.fullmatch(r"BV[A-Za-z0-9]{10}", media_id):
            return f"https://www.bilibili.com/video/{media_id}?p={ordinal}"
    if site == "bilibili" and re.search(r"/video/BV[A-Za-z0-9]{10}", parent_url):
        return parent_url.split("?", 1)[0] + f"?p={ordinal}"
    return None


async def resolve_collection_page(url: str, site: str, cookie_file: Path | None, start: int, limit: int = 100) -> dict | None:
    """Read at most one flat page. A non-collection returns None."""
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise ApiError(503, "DEPENDENCY_MISSING", "yt-dlp is not installed")
    args = [executable, "--ignore-config", "--flat-playlist", "--lazy-playlist", "--playlist-items", f"{start}:{start + limit - 1}", "--dump-json", "--no-warnings", "--socket-timeout", "30"]
    if cookie_file is not None:
        args.extend(["--cookies", str(cookie_file)])
    args.extend(["--", url])
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = await asyncio.wait_for(asyncio.gather(_bounded_stdout(process.stdout), process.wait()), timeout=120)
    except (OutputTooLarge, asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ApiError(422 if isinstance(exc, OutputTooLarge) else 504, "ANALYSIS_TOO_LARGE" if isinstance(exc, OutputTooLarge) else "ANALYSIS_TIMEOUT", "Collection analysis did not finish") from exc
    if process.returncode != 0:
        raise ApiError(422, "ANALYSIS_FAILED", "Collection analysis failed")
    try:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(502, "ANALYSIS_INVALID", "Collection analysis returned invalid metadata") from exc
    if not rows and start > 1:
        return {"site": site, "collection": True, "title": "Collection", "items": [], "next_index": None}
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ApiError(502, "ANALYSIS_INVALID", "Collection analysis returned invalid metadata")
    if len(rows) == 1 and not rows[0].get("playlist_id") and rows[0].get("_type") not in {"playlist", "multi_video"}:
        return None
    items = []
    for offset, row in enumerate(rows[:limit]):
        ordinal = row.get("playlist_index") if isinstance(row.get("playlist_index"), int) and row["playlist_index"] > 0 else start + offset
        duration = row.get("duration") if isinstance(row.get("duration"), (int, float)) and row["duration"] >= 0 else None
        items.append({"ordinal": ordinal, "title": str(row.get("title") or f"Item {ordinal}")[:255],
                      "duration_seconds": duration, "source_url": _collection_entry_url(row, site, url, ordinal)})
    return {"site": site, "collection": True, "title": str(rows[0].get("playlist_title") or rows[0].get("playlist") or "Collection")[:255],
            "items": items, "next_index": start + len(rows) if len(rows) >= limit else None}


async def resolve_video(url: str, site: str, cookie_file: Path | None = None) -> dict:
    executable = shutil.which("yt-dlp")
    if executable is None:
        raise ApiError(503, "DEPENDENCY_MISSING", "yt-dlp is not installed")
    args = [executable, "--ignore-config", "--no-playlist", "--skip-download", "--dump-single-json", "--no-warnings", "--socket-timeout", "30"]
    if cookie_file is not None:
        args.extend(["--cookies", str(cookie_file)])
    args.extend(["--", url])
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        stdout, _return_code = await asyncio.wait_for(asyncio.gather(_bounded_stdout(process.stdout), process.wait()), timeout=120)
    except OutputTooLarge as exc:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        raise ApiError(422, "ANALYSIS_TOO_LARGE", "Video metadata is too large") from exc
    except asyncio.TimeoutError as exc:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise ApiError(504, "ANALYSIS_TIMEOUT", "Video analysis timed out") from exc
    except asyncio.CancelledError:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        raise
    if process.returncode != 0:
        raise ApiError(422, "ANALYSIS_FAILED", "Video analysis failed; check the URL, Cookie profile, or site access")
    try:
        info = json.loads(stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(502, "ANALYSIS_INVALID", "Video analysis returned invalid metadata") from exc
    if not isinstance(info, dict):
        raise ApiError(502, "ANALYSIS_INVALID", "Video analysis returned invalid metadata")
    return summarize_video(info, site)


async def probe_media(path: Path) -> bool:
    executable = shutil.which("ffprobe")
    if executable is None:
        return False
    process = await asyncio.create_subprocess_exec(
        executable, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        return False
    if process.returncode != 0 or len(stdout) > 65536:
        return False
    try:
        streams = json.loads(stdout).get("streams", [])
    except (ValueError, UnicodeDecodeError, AttributeError):
        return False
    return any(isinstance(stream, dict) and stream.get("codec_type") in {"audio", "video"} for stream in streams)
