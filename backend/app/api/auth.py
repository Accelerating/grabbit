from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select

from app.api.deps import AuthSession, CsrfSession, Db
from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.origin import origin_allowed
from app.core.security import hash_password, hash_token, new_token, verify_password
from app.db.models import Session, User

router = APIRouter(prefix="/auth", tags=["auth"])
password_verify_lock = asyncio.Lock()
login_attempts: dict[str, deque[float]] = {}
login_attempts_lock = asyncio.Lock()


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=6, max_length=1024)


def session_payload(record: Session, csrf_token: str) -> dict:
    return {
        "user": {"id": record.user.id, "username": record.user.username},
        "csrf_token": csrf_token,
        "expires_at": record.expires_at.isoformat(),
    }


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Db,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    if not origin_allowed(request, settings):
        raise ApiError(403, "CSRF_FAILED", "Request origin is not allowed")
    client_key = request.client.host if request.client else "unknown"
    async with login_attempts_lock:
        now = time.monotonic()
        if len(login_attempts) > 1024:
            for key, recent in list(login_attempts.items()):
                if not recent or now - recent[-1] > 300:
                    login_attempts.pop(key, None)
            if len(login_attempts) > 1024:
                login_attempts.pop(next(iter(login_attempts)))
        attempts = login_attempts.setdefault(client_key, deque())
        while attempts and now - attempts[0] > 300:
            attempts.popleft()
        if len(attempts) >= 5:
            raise ApiError(429, "RATE_LIMITED", "Too many login attempts; try again later")
        attempts.append(now)
    user = await db.scalar(select(User).where(User.username == body.username))
    async with password_verify_lock:
        valid = user is not None and await asyncio.to_thread(verify_password, user.password_hash, body.password)
    if not valid or user is None:
        raise ApiError(401, "INVALID_CREDENTIALS", "Username or password is incorrect")
    async with login_attempts_lock:
        login_attempts.pop(client_key, None)
    raw_token, csrf_token = new_token(), new_token()
    record = Session(
        user=user,
        token_hash=hash_token(raw_token),
        csrf_token_hash=hash_token(csrf_token),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.session_days),
    )
    db.add(record)
    await db.commit()
    response.set_cookie(
        "grabbit_session", raw_token, httponly=True, secure=settings.secure_cookies,
        samesite="lax", max_age=settings.session_days * 86400, path="/",
    )
    return session_payload(record, csrf_token)


@router.get("/session")
async def get_current_session(auth: AuthSession, db: Db) -> dict:
    csrf_token = new_token()
    auth.csrf_token_hash = hash_token(csrf_token)
    await db.commit()
    return session_payload(auth, csrf_token)


@router.post("/logout", status_code=204)
async def logout(response: Response, db: Db, auth: CsrfSession, request: Request) -> Response:
    user_id = auth.user_id
    await db.delete(auth)
    await db.commit()
    bus = getattr(request.app.state, "event_bus", None)
    if bus:
        await bus.disconnect_user(user_id)
    response.delete_cookie("grabbit_session", path="/")
    response.status_code = 204
    return response


@router.patch("/password", status_code=204)
async def change_password(body: PasswordRequest, response: Response, db: Db, auth: CsrfSession, request: Request) -> Response:
    async with password_verify_lock:
        valid = await asyncio.to_thread(verify_password, auth.user.password_hash, body.current_password)
    if not valid:
        raise ApiError(401, "INVALID_CREDENTIALS", "Current password is incorrect")
    user_id = auth.user_id
    auth.user.password_hash = await asyncio.to_thread(hash_password, body.new_password)
    await db.execute(delete(Session).where(Session.user_id == auth.user_id))
    await db.commit()
    bus = getattr(request.app.state, "event_bus", None)
    if bus:
        await bus.disconnect_user(user_id)
    response.delete_cookie("grabbit_session", path="/")
    response.status_code = 204
    return response
