# Debian 13 VPS deployment record (2026-09-17)

Public site: `https://vido.zhcn.win/`. The deployment pulled GitHub commit
`7e63b6f` into a versioned release and points `/opt/grabbit/current` at it.
No server password, GitHub token, or initial administrator password is stored
in Git. `docs/deploy-server.properties` is explicitly ignored.

The host reports Debian 13 x86_64, 715 MiB RAM, 768 MiB swap and a 14 GiB
root filesystem. The release has Python 3.13.5 and backend dependencies from
`uv.lock`; the browser bundle was built off-host. Debian packages provide
aria2, ffmpeg/ffprobe and Caddy 2.6.2. A separate Python tool environment
provides yt-dlp 2026.08.19 with its matched yt-dlp-ejs 0.8.0; Deno 2.7.0 was
installed from a GitHub release archive after SHA-256 verification.

`grabbit.service` and `caddy.service` are enabled and active. The backend
listens only at `127.0.0.1:8000`; Caddy serves ports 80 and 443 with automatic
HTTPS. The public homepage and `/healthz` returned 200 with a valid TLS
certificate. An authenticated HTTPS login returned 200 and the dependency API
reported aria2, yt-dlp, ffmpeg, ffprobe and Deno available. Database migration,
SQLite integrity check, service restart and the exact-command sudoers check
passed. The database file is mode 0600, environment file 0640, and private
state directory 0700.

An initial 30-second idle cgroup sample on this host recorded:

| Measure | Result |
| --- | ---: |
| Samples | 30 |
| Maximum sampled memory | 84,283,392 bytes |
| Cgroup peak memory | 86,228,992 bytes |
| Additional OOM kills | 0 |
| Failed localhost health checks | 0 |
| Maximum observed health latency | 35.6 ms |

CSV evidence is at `/var/lib/grabbit/reports/idle-20260917.csv` on the VPS.
This is an idle baseline only, **not** the required 768 MiB typical-download
acceptance. A sustained real download, website package installation, video
site tests, automatic first-install script, and yt-dlp rollback remain
unverified or unfinished.
