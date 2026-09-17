"""Validation and private immutable storage for Netscape cookies.txt profiles."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

MAX_COOKIE_BYTES = 1024 * 1024
DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SITE_KEY = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")
COOKIE_KEY = re.compile(r"^cookies/([0-9a-f-]{36})\.([1-9][0-9]*)\.txt$")


class CookieValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedCookies:
    entry_count: int
    domains: tuple[str, ...]


def normalize_site_key(value: str) -> str:
    key = value.strip().lower()
    if not SITE_KEY.fullmatch(key):
        raise CookieValidationError("Invalid site key")
    return key


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().lstrip(".")
    if len(domain) > 253 or not domain or any(not DOMAIN_LABEL.fullmatch(label) for label in domain.split(".")):
        raise CookieValidationError("Invalid domain scope")
    return domain


def parse_domain_scope(value: str) -> tuple[str, ...]:
    domains = tuple(dict.fromkeys(normalize_domain(item) for item in value.split(",") if item.strip()))
    if not domains or len(domains) > 32:
        raise CookieValidationError("Provide 1–32 valid domains")
    return domains


def parse_cookie_text(blob: bytes, allowed_domains: tuple[str, ...]) -> ParsedCookies:
    if not blob or len(blob) > MAX_COOKIE_BYTES or b"\x00" in blob:
        raise CookieValidationError("Cookie file must be 1 byte to 1 MiB")
    try:
        content = blob.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CookieValidationError("Cookie file must be UTF-8") from exc
    count = 0
    seen_domains: set[str] = set()
    for line in content.splitlines():
        if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise CookieValidationError("Cookie file is not Netscape cookies.txt format")
        raw_domain = fields[0].removeprefix("#HttpOnly_")
        domain = normalize_domain(raw_domain)
        if not any(domain == allowed or domain.endswith("." + allowed) for allowed in allowed_domains):
            raise CookieValidationError("Cookie domain is outside the declared scope")
        if fields[1].upper() not in {"TRUE", "FALSE"} or fields[3].upper() not in {"TRUE", "FALSE"}:
            raise CookieValidationError("Invalid cookie flag")
        if not fields[2].startswith("/") or not fields[4].isdigit() or not fields[5]:
            raise CookieValidationError("Invalid cookie entry")
        if any(ord(char) < 32 or ord(char) == 127 for part in fields for char in part):
            raise CookieValidationError("Cookie entry contains control characters")
        count += 1
        seen_domains.add(domain)
    if count == 0:
        raise CookieValidationError("Cookie file has no entries")
    return ParsedCookies(entry_count=count, domains=tuple(sorted(seen_domains)))


def cookie_blob_path(private_root: Path, key: str) -> Path:
    match = COOKIE_KEY.fullmatch(key)
    if not match:
        raise ValueError("Invalid cookie storage key")
    UUID(match.group(1))
    return private_root / key


def persist_cookie_blob(private_root: Path, profile_id: str, version: int, blob: bytes) -> str:
    UUID(profile_id)
    if version < 1 or len(blob) > MAX_COOKIE_BYTES:
        raise ValueError("Invalid cookie version or size")
    directory = private_root / "cookies"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    key = f"cookies/{profile_id}.{version}.txt"
    target = cookie_blob_path(private_root, key)
    if target.exists():
        raise FileExistsError("Cookie version already exists")
    fd, temp_name = tempfile.mkstemp(prefix=f".{profile_id}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return key
