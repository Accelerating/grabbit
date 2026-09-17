"""Cookie profile metadata endpoints; secret contents never leave the server."""
from __future__ import annotations

import json
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text, update

from app.api.deps import AuthSession, CsrfSession, Db
from app.api.tasks import ACTIVE
from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.models import CookieProfile, SiteCookieDefault, Task, utcnow
from app.db.session import SessionLocal
from app.services.cookies import (
    MAX_COOKIE_BYTES, CookieValidationError, cookie_blob_path,
    normalize_site_key, parse_cookie_text, parse_domain_scope, persist_cookie_blob,
)

router = APIRouter(prefix="/cookies", tags=["cookies"])


def profile_json(profile: CookieProfile, defaults: set[str] | None = None) -> dict:
    return {
        "id": profile.id, "name": profile.name, "site_key": profile.site_key,
        "domains": json.loads(profile.domain_scope_json),
        "content_version": profile.content_version, "entry_count": profile.entry_count,
        "last_result_code": profile.last_result_code,
        "last_used_at": profile.last_used_at.isoformat() if profile.last_used_at else None,
        "created_at": profile.created_at.isoformat(), "updated_at": profile.updated_at.isoformat(),
        "is_default": profile.site_key in defaults if defaults is not None else False,
    }


def _validated_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 255 or any(ord(char) < 32 for char in name):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid cookie profile name")
    return name


def _cookie_error(exc: CookieValidationError) -> ApiError:
    # Validation details describe only structure/scope, never values.
    return ApiError(422, "INVALID_COOKIE_FILE", str(exc))


async def _content(file: UploadFile | None, text_value: str | None) -> bytes:
    if (file is None) == (text_value is None):
        raise ApiError(422, "VALIDATION_ERROR", "Provide either file or text")
    if file is not None:
        blob = await file.read(MAX_COOKIE_BYTES + 1)
        await file.close()
    else:
        blob = text_value.encode("utf-8") if text_value is not None else b""
    if not blob or len(blob) > MAX_COOKIE_BYTES:
        raise ApiError(413, "COOKIE_TOO_LARGE", "Cookie content must be 1 byte to 1 MiB")
    return blob


@router.get("")
async def list_cookies(db: Db, auth: AuthSession) -> dict:
    rows = (await db.scalars(select(CookieProfile).order_by(CookieProfile.updated_at.desc()))).all()
    defaults = {row.site_key: row.cookie_profile_id for row in (await db.scalars(select(SiteCookieDefault))).all()}
    return {"items": [profile_json(row, {row.site_key} if defaults.get(row.site_key) == row.id else set()) for row in rows]}


@router.post("", status_code=201)
async def create_cookie(
    auth: CsrfSession,
    name: Annotated[str, Form()],
    site_key: Annotated[str, Form()],
    domains: Annotated[str, Form()],
    file: Annotated[UploadFile | None, File()] = None,
    text_value: Annotated[str | None, Form(alias="text")] = None,
) -> dict:
    clean_name = _validated_name(name)
    try:
        site = normalize_site_key(site_key)
        scope = parse_domain_scope(domains)
        blob = await _content(file, text_value)
        parsed = parse_cookie_text(blob, scope)
    except CookieValidationError as exc:
        raise _cookie_error(exc) from exc
    profile_id = str(uuid4())
    settings = get_settings()
    try:
        key = persist_cookie_blob(settings.private_root, profile_id, 1, blob)
    except OSError as exc:
        raise ApiError(507, "STORAGE_UNAVAILABLE", "Unable to store cookie profile") from exc
    now = utcnow()
    async with SessionLocal() as db:
        db.add(CookieProfile(id=profile_id, name=clean_name, site_key=site, domain_scope_json=json.dumps(scope), secret_file_key=key, content_version=1, entry_count=parsed.entry_count, created_at=now, updated_at=now))
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            cookie_blob_path(settings.private_root, key).unlink(missing_ok=True)
            raise
    return {"id": profile_id, "name": clean_name, "site_key": site, "domains": list(scope), "content_version": 1, "entry_count": parsed.entry_count, "last_result_code": None, "last_used_at": None, "created_at": now.isoformat(), "updated_at": now.isoformat(), "is_default": False}


class CookiePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=255)
    domains: list[str] | None = Field(default=None, min_length=1, max_length=32)


@router.patch("/{profile_id}")
async def patch_cookie(profile_id: str, body: CookiePatch, auth: CsrfSession) -> dict:
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        profile = await db.get(CookieProfile, profile_id)
        if profile is None:
            raise ApiError(404, "NOT_FOUND", "Cookie profile not found")
        if body.name is not None:
            profile.name = _validated_name(body.name)
        if body.domains is not None:
            try:
                scope = parse_domain_scope(",".join(body.domains))
                blob = cookie_blob_path(get_settings().private_root, profile.secret_file_key).read_bytes()
                parse_cookie_text(blob, scope)
            except CookieValidationError as exc:
                raise _cookie_error(exc) from exc
            except OSError as exc:
                raise ApiError(507, "STORAGE_UNAVAILABLE", "Cookie content is unavailable") from exc
            profile.domain_scope_json = json.dumps(scope)
        profile.updated_at = utcnow()
        await db.commit()
        default = await db.get(SiteCookieDefault, profile.site_key)
        return profile_json(profile, {profile.site_key} if default and default.cookie_profile_id == profile.id else set())


@router.put("/{profile_id}/content")
async def replace_cookie_content(
    profile_id: str, auth: CsrfSession,
    file: Annotated[UploadFile | None, File()] = None,
    text_value: Annotated[str | None, Form(alias="text")] = None,
) -> dict:
    blob = await _content(file, text_value)
    settings = get_settings()
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        profile = await db.get(CookieProfile, profile_id)
        if profile is None:
            raise ApiError(404, "NOT_FOUND", "Cookie profile not found")
        try:
            parsed = parse_cookie_text(blob, tuple(json.loads(profile.domain_scope_json)))
        except CookieValidationError as exc:
            raise _cookie_error(exc) from exc
        next_version = profile.content_version + 1
        try:
            key = persist_cookie_blob(settings.private_root, profile.id, next_version, blob)
        except OSError as exc:
            raise ApiError(507, "STORAGE_UNAVAILABLE", "Unable to store cookie content") from exc
        profile.secret_file_key = key
        profile.content_version = next_version
        profile.entry_count = parsed.entry_count
        profile.updated_at = utcnow()
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            cookie_blob_path(settings.private_root, key).unlink(missing_ok=True)
            raise
        default = await db.get(SiteCookieDefault, profile.site_key)
        return profile_json(profile, {profile.site_key} if default and default.cookie_profile_id == profile.id else set())


class DefaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cookie_profile_id: str | None = None


@router.put("/defaults/{site_key}")
async def set_site_default(site_key: str, body: DefaultRequest, auth: CsrfSession) -> dict:
    try:
        site = normalize_site_key(site_key)
    except CookieValidationError as exc:
        raise _cookie_error(exc) from exc
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        if body.cookie_profile_id is not None:
            profile = await db.get(CookieProfile, body.cookie_profile_id)
            if profile is None or profile.site_key != site:
                raise ApiError(422, "VALIDATION_ERROR", "Cookie profile does not belong to this site")
        default = await db.get(SiteCookieDefault, site)
        if body.cookie_profile_id is None:
            if default:
                await db.delete(default)
        elif default:
            default.cookie_profile_id = body.cookie_profile_id
        else:
            db.add(SiteCookieDefault(site_key=site, cookie_profile_id=body.cookie_profile_id))
        await db.commit()
        return {"site_key": site, "cookie_profile_id": body.cookie_profile_id}


@router.delete("/{profile_id}", status_code=204)
async def delete_cookie(profile_id: str, auth: CsrfSession) -> Response:
    settings = get_settings()
    async with SessionLocal() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        profile = await db.get(CookieProfile, profile_id)
        if profile is None:
            raise ApiError(404, "NOT_FOUND", "Cookie profile not found")
        in_use = await db.scalar(select(Task.id).where(Task.cookie_profile_id == profile_id, Task.status.in_(ACTIVE)).limit(1))
        if in_use is not None:
            raise ApiError(409, "COOKIE_IN_USE", "Cookie profile is used by an active task")
        await db.execute(update(Task).where(Task.cookie_profile_id == profile_id).values(cookie_profile_id=None, cookie_name_snapshot=profile.name))
        defaults = (await db.scalars(select(SiteCookieDefault).where(SiteCookieDefault.cookie_profile_id == profile_id))).all()
        for default in defaults:
            await db.delete(default)
        await db.delete(profile)
        await db.commit()
    directory = settings.private_root / "cookies"
    for path in directory.glob(f"{profile_id}.*.txt"):
        if path.is_file() and not path.is_symlink():
            try:
                path.unlink()
            except OSError:
                pass
    return Response(status_code=204)
