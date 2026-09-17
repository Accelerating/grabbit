from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import Settings


def database_path(settings: Settings) -> Path:
    url = make_url(settings.database_url)
    if url.drivername != "sqlite+aiosqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Maintenance requires a file-backed SQLite database")
    return Path(url.database).absolute()


def _validate_tree(root: Path) -> None:
    if not root.exists():
        return
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"Private state contains a non-regular entry: {path}")


def create_backup(settings: Settings, output: Path, env_file: Path | None = None) -> Path:
    """Create an offline-friendly consistent SQLite snapshot and private-state copy."""
    db_path = database_path(settings)
    private = settings.private_root
    output = output.absolute()
    if not output.is_absolute() or output.exists():
        raise ValueError("Backup output must be a new directory")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise ValueError("Backup parent must be an existing real directory")
    if output.is_relative_to(private) or output.is_relative_to(settings.download_root):
        raise ValueError("Backup must be outside private and download roots")
    if private.is_relative_to(output) or settings.download_root.is_relative_to(output):
        raise ValueError("Backup cannot contain live application roots")
    if not db_path.is_file() or db_path.is_symlink():
        raise ValueError("SQLite database is missing or is a symlink")
    _validate_tree(private)
    if env_file and (not env_file.is_file() or env_file.is_symlink()):
        raise ValueError("Environment file is missing or is a symlink")
    output.mkdir(mode=0o700)
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as source:
            with sqlite3.connect(output / "grabbit.db") as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup SQLite integrity check failed")
        os.chmod(output / "grabbit.db", 0o600)
        if private.exists():
            shutil.copytree(private, output / "private", symlinks=False)
            for path in (output / "private").rglob("*"):
                os.chmod(path, 0o700 if path.is_dir() else 0o600)
        if env_file:
            shutil.copyfile(env_file, output / "grabbit.env")
            os.chmod(output / "grabbit.env", 0o600)
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": "grabbit.db", "private_state": private.exists(),
            "environment": bool(env_file), "downloads_included": False,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.chmod(output / "manifest.json", 0o600)
    except Exception:
        shutil.rmtree(output)
        raise
    return output


def doctor(settings: Settings) -> dict:
    db_path = database_path(settings)
    checks: dict[str, str] = {}
    for name, path in (("database", db_path), ("private_root", settings.private_root), ("download_root", settings.download_root)):
        checks[name] = "ok" if path.exists() and os.access(path, os.R_OK | (os.W_OK if name != "database" else 0)) else "missing_or_inaccessible"
    if checks["database"] == "ok":
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
                checks["sqlite_integrity"] = connection.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as exc:
            checks["sqlite_integrity"] = type(exc).__name__
    for name, executable in (("aria2", "aria2c"), ("yt-dlp", "yt-dlp"), ("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe"), ("deno", "deno")):
        checks[name] = "available" if shutil.which(executable) else "missing"
    return checks
