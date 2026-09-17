# Grabbit backend (phase 0)

The backend targets Python 3.13. Run from this directory:

```sh
uv sync --python 3.13
uv run python -m app.cli db upgrade
uv run python -m app.cli admin create
GRABBIT_PUBLIC_ORIGIN=http://127.0.0.1:8765 uv run uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
```

Set `GRABBIT_DATABASE_URL`, `GRABBIT_DOWNLOAD_ROOT`, `GRABBIT_PRIVATE_ROOT`, `GRABBIT_PUBLIC_ORIGIN`, and `GRABBIT_SECURE_COOKIES` in the deployment environment. Production requires HTTPS, secure cookies, and an exact configured public Origin. For local development only, loopback access on a different port is accepted when the browser's Origin matches its actual loopback Host and port. The SQLite parent directory must already exist and be writable by the service user before migration. Passwords may be 6 characters for local testing; use a longer unique password if the service is exposed.

`uv run python -m app.cli admin reset-password` interactively changes the password and revokes every session. Neither CLI command accepts passwords as command-line arguments.

The backend now includes HTTP, torrent, magnet, and single-video download flows, Cookie profiles, file browsing and playback, and persistent background video analysis. Install `aria2c`, `yt-dlp`, `ffmpeg`, and `ffprobe` for their corresponding features; the Settings page reports dependency availability. After pulling updates, run `uv run python -m app.cli db upgrade` before starting the service. Playlist support, durable batch file operations, web installation, and Debian/768 MB acceptance remain unfinished; do not expose this deployment as a finished download service.
