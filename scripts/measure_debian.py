#!/usr/bin/env python3
"""Sample Grabbit's systemd cgroup on a Debian 13 acceptance host."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def cgroup_path() -> Path:
    result = subprocess.run(
        ["systemctl", "show", "grabbit.service", "--property=ControlGroup", "--value"],
        check=True, capture_output=True, text=True, timeout=5,
    )
    relative = result.stdout.strip()
    if not relative.startswith("/system.slice/") or ".." in relative.split("/"):
        raise ValueError("Unexpected Grabbit cgroup")
    path = Path("/sys/fs/cgroup") / relative.lstrip("/")
    if not (path / "memory.current").is_file():
        raise ValueError("cgroup v2 memory.current is unavailable")
    return path


def sample(path: Path) -> dict:
    started = time.monotonic()
    try:
        with urlopen("http://127.0.0.1:8000/healthz", timeout=2) as response:
            http_status = response.status
        latency_ms = round((time.monotonic() - started) * 1000, 1)
    except (OSError, URLError, TimeoutError):
        http_status, latency_ms = None, None
    events = {}
    if (path / "memory.events").is_file():
        events = dict(line.split() for line in (path / "memory.events").read_text().splitlines())
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "memory_current_bytes": int((path / "memory.current").read_text()),
        "memory_peak_bytes": int((path / "memory.peak").read_text()) if (path / "memory.peak").is_file() else None,
        "memory_limit_bytes": (path / "memory.max").read_text().strip(),
        "oom_count": int(events.get("oom", 0)),
        "oom_kill_count": int(events.get("oom_kill", 0)),
        "pids_current": int((path / "pids.current").read_text()) if (path / "pids.current").is_file() else None,
        "health_status": http_status, "health_latency_ms": latency_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or args.output.exists():
        parser.error("Use 1–3600 seconds and a new output path")
    path = cgroup_path()
    rows = []
    for index in range(args.seconds):
        rows.append(sample(path))
        if index + 1 < args.seconds:
            time.sleep(1)
    with args.output.open("x", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({
        "samples": len(rows),
        "max_memory_current_bytes": max(row["memory_current_bytes"] for row in rows),
        "max_memory_peak_bytes": max((row["memory_peak_bytes"] or 0) for row in rows),
        "oom_kills_delta": rows[-1]["oom_kill_count"] - rows[0]["oom_kill_count"],
        "health_failures": sum(row["health_status"] != 200 for row in rows),
        "max_health_latency_ms": max((row["health_latency_ms"] or 0) for row in rows),
        "csv": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
