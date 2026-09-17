from __future__ import annotations

import json
import sqlite3

import pytest

from app.core.config import Settings
from app.services.maintenance import create_backup, doctor


def test_backup_contains_consistent_database_and_private_state(tmp_path):
    database = tmp_path / "live.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE example (value TEXT)")
        connection.execute("INSERT INTO example VALUES ('saved')")
    private = tmp_path / "private"
    private.mkdir()
    (private / "cookie.txt").write_text("secret")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "big.bin").write_bytes(b"large media")
    env = tmp_path / "grabbit.env"
    env.write_text("SECRET=yes\n")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database}", private_root=private, download_root=downloads)
    output = create_backup(settings, tmp_path / "backup", env)
    with sqlite3.connect(output / "grabbit.db") as connection:
        assert connection.execute("SELECT value FROM example").fetchone() == ("saved",)
    assert (output / "private/cookie.txt").read_text() == "secret"
    assert (output / "grabbit.env").read_text() == "SECRET=yes\n"
    assert not (output / "downloads").exists()
    assert json.loads((output / "manifest.json").read_text())["downloads_included"] is False
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "private/cookie.txt").stat().st_mode & 0o777 == 0o600
    assert doctor(settings)["sqlite_integrity"] == "ok"
    with pytest.raises(ValueError):
        create_backup(settings, output)


def test_backup_rejects_private_symlinks(tmp_path):
    database = tmp_path / "live.db"
    sqlite3.connect(database).close()
    private = tmp_path / "private"
    private.mkdir()
    (private / "leak").symlink_to(tmp_path / "outside")
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database}", private_root=private, download_root=tmp_path / "downloads")
    with pytest.raises(ValueError, match="non-regular"):
        create_backup(settings, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
