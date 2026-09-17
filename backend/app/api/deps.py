from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import Cookie, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.origin import origin_allowed
from app.core.security import constant_time_token_matches, hash_token
from app.db.models import Session
from app.db.session import get_session

Db = Annotated[AsyncSession, Depends(get_session)]


async def current_session(
    db: Db,
    session_token: Annotated[str | None, Cookie(alias="grabbit_session")] = None,
) -> Session:
    if not session_token:
        raise ApiError(401, "AUTH_REQUIRED", "Authentication is required")
    record = await db.scalar(
        select(Session).where(Session.token_hash == hash_token(session_token)).options(selectinload(Session.user))
    )
    now = datetime.now(timezone.utc)
    if record is None or record.user is None or record.expires_at.replace(tzinfo=timezone.utc) <= now:
        raise ApiError(401, "AUTH_REQUIRED", "Authentication is required")
    return record


AuthSession = Annotated[Session, Depends(current_session)]


async def require_csrf(
    request: Request,
    auth: AuthSession,
    x_csrf_token: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,
) -> Session:
    if not origin_allowed(request, settings):
        raise ApiError(403, "CSRF_FAILED", "Request origin is not allowed")
    if not x_csrf_token or not constant_time_token_matches(x_csrf_token, auth.csrf_token_hash):
        raise ApiError(403, "CSRF_FAILED", "CSRF token is invalid")
    return auth


CsrfSession = Annotated[Session, Depends(require_csrf)]
