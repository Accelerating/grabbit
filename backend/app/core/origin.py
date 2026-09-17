from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from fastapi import Request

from app.core.config import Settings


def _loopback_hostname(value: str | None) -> bool:
    if not value:
        return False
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def origin_allowed(request: Request, settings: Settings) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    if origin == settings.public_origin:
        return True

    # Local development may use a different port or switch between localhost
    # and 127.0.0.1. Never extend this convenience to a public origin or a
    # non-loopback client, and still require exact same-origin host+port.
    configured = urlsplit(settings.public_origin)
    actual = urlsplit(origin)
    host = urlsplit(f"http://{request.headers.get('host', '')}")
    client = request.client.host if request.client else None
    return (
        not settings.secure_cookies
        and configured.scheme == "http"
        and _loopback_hostname(configured.hostname)
        and _loopback_hostname(client)
        and actual.scheme == "http"
        and _loopback_hostname(actual.hostname)
        and _loopback_hostname(host.hostname)
        and actual.netloc.lower() == host.netloc.lower()
        and actual.path == ""
        and actual.query == ""
        and actual.fragment == ""
    )
