# Grabbit deployment handoff

The repository is deployed as one release: the backend and the frontend's
browser bundle live together under `/opt/grabbit/current`. Node is needed on a
developer/CI machine only; the VPS runs Uvicorn, Nginx, SQLite, and the
download tools.

## Build a release

From the repository root, on a machine with Node and pnpm:

```sh
./scripts/build_frontend.sh
```

The script runs `pnpm install --frozen-lockfile`, builds the React Router client,
and atomically replaces `backend/app/static/`. If dependencies are already
installed, use `SKIP_INSTALL=1 ./scripts/build_frontend.sh`. Copy the resulting
`backend/` directory (including the built `app/static/`) into a versioned
release, install the backend's locked Python dependencies into that release's
`.venv`, then atomically point `/opt/grabbit/current` at it.
Use Python 3.13 (`uv sync --python 3.13 --locked`) for the backend.

## Configure the service

The first-deployment procedure must create the `grabbit` user and the paths in
`docs/implementation-plan.md`. Copy and edit the environment file:

```sh
sudo install -d -o root -g grabbit -m 0750 /etc/grabbit
sudo install -o root -g grabbit -m 0640 deploy/grabbit.env.example /etc/grabbit/grabbit.env
sudoedit /etc/grabbit/grabbit.env
```

Install the unit after the release and its virtual environment exist:

```sh
sudo install -o root -g root -m 0644 deploy/systemd/grabbit.service /etc/systemd/system/grabbit.service
sudo systemctl daemon-reload
sudo systemctl enable --now grabbit.service
sudo systemctl status grabbit.service
```

The unit intentionally uses one worker, binds only to `127.0.0.1`, and kills
the whole child process group on stop. It does not set `NoNewPrivileges` or
`ProtectSystem`: both would prevent the narrowly-authorized sudo helper from
performing apt writes in the same service mount namespace. The unprivileged
service account itself cannot write system paths or the root-owned release.
Restore stronger systemd filesystem restrictions if privileged package
installation is moved into a separate broker.

## Configure Caddy and HTTPS

Install the official Caddy Debian package, replace the example domain in
`deploy/caddy/Caddyfile`, then validate and install it as `/etc/caddy/Caddyfile`.
Keep ports 80 and 443 open and point DNS at the server; Caddy obtains and
renews the HTTPS certificate automatically. Keep
`GRABBIT_PUBLIC_ORIGIN=https://your-domain` and
`GRABBIT_SECURE_COOKIES=true` in the application environment.

The Caddyfile forwards Range/If-Range and streams SSE through the reverse
proxy. The Debian 13 Caddy package is older than the `request_body max_size`
directive; upload limits remain enforced by the application. It does not expose the private or
download roots as public static files; protected content stays behind FastAPI.
For a first check without a public domain, use an SSH tunnel to
`127.0.0.1:8000` with an explicit, temporary development override.

## Debian 13 dependencies and maintenance

Install the restricted helper as root, not from the writable application release:

```sh
sudo install -d -o root -g root -m 0755 /usr/local/libexec
sudo install -o root -g root -m 0755 deploy/grabbit-install /usr/local/libexec/grabbit-install
sudo install -o root -g root -m 0440 deploy/sudoers/grabbit-install /etc/sudoers.d/grabbit-install
sudo visudo -cf /etc/sudoers.d/grabbit-install
```

The helper accepts only `aria2` or `ffmpeg`, rejects other operating systems,
and invokes apt with fixed package names. These packages are available in
Debian 13; the ffmpeg package includes ffprobe. The sudoers entry grants only
those exact two commands. It does not authorize yt-dlp, Deno, or arbitrary
packages. Once installed, the settings page can queue an aria2 or ffmpeg job:
dispatch pauses, existing tasks drain, a memory/disk guard runs, and a bounded
log is kept. This flow has unit tests but still requires Debian-host acceptance.

The backend requires Python 3.13. On a clean host install `python3.13-venv`,
`ca-certificates`, `sudo`, and a way to install the `uv` executable using your
organization's approved package workflow. Build the frontend off-host, then
run `uv sync --python 3.13 --locked --no-dev` in the deployed backend directory.
Do not copy a macOS virtual environment to the VPS.

For a consistent backup while the service may be running:

```sh
sudo install -d -o grabbit -g grabbit -m 0700 /var/lib/grabbit/backups
cd /opt/grabbit/current/backend
sudo -u grabbit .venv/bin/python -m app.cli backup --output /var/lib/grabbit/backups/backup-YYYYMMDD --env-file /etc/grabbit/grabbit.env
```

Run the CLI from `/opt/grabbit/current/backend` (the systemd working directory).
The backup uses SQLite's online backup API, includes the private Cookie/torrent
state and optional environment file, and deliberately excludes media under
`/srv/grabbit/downloads`. Protect the resulting directory like a secret.
`python -m app.cli doctor` checks paths, SQLite integrity and local executables.
Restoration should be performed with the service stopped: copy `grabbit.db`,
`private/`, and `grabbit.env` from a known-good backup to their configured
locations with the documented ownership/modes, then start the service and
check `doctor` and `/healthz`. Do not run an older release against a newer
schema without checking migration compatibility.

The first-install workflow, yt-dlp version rollback, and 768 MB Debian host
acceptance are still pending; this document is not a claim that a
fresh-host deployment has been validated.

For the 768 MB acceptance run, start the service, collect an idle baseline,
then repeat while creating one representative HTTP or video task:

```sh
python3 scripts/measure_debian.py --seconds 120 --output idle.csv
python3 scripts/measure_debian.py --seconds 300 --output single-download.csv
```

Run this from a release checkout containing `scripts/`, or copy the script to
the host. It records the whole `grabbit.service` cgroup (including aria2,
yt-dlp, and ffmpeg children), OOM counters, and localhost health latency. Keep
both CSV files and the command summaries in the performance report; an idle
desktop measurement is not a substitute for the 768 MB VPS result.
