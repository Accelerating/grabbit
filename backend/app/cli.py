from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select

from app.core.security import hash_password
from app.core.config import get_settings
from app.services.maintenance import create_backup, doctor
from app.db.models import Session, User
from app.db.session import SessionLocal


def migrate() -> None:
    config = Config("alembic.ini")
    command.upgrade(config, "head")


async def create_admin() -> None:
    username = input("Admin username: ").strip()
    if not username or len(username) > 64:
        raise SystemExit("Username must be 1–64 characters")
    password = getpass.getpass("Password (at least 6 characters): ")
    if len(password) < 6 or password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match or are too short")
    async with SessionLocal() as db:
        if await db.scalar(select(User.id).limit(1)):
            raise SystemExit("An administrator already exists")
        db.add(User(username=username, password_hash=await asyncio.to_thread(hash_password, password)))
        await db.commit()
    print("Administrator created")


async def reset_password() -> None:
    password = getpass.getpass("New password (at least 6 characters): ")
    if len(password) < 6 or password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match or are too short")
    async with SessionLocal() as db:
        user = await db.scalar(select(User).limit(1))
        if user is None:
            raise SystemExit("Create an administrator first")
        user.password_hash = await asyncio.to_thread(hash_password, password)
        await db.execute(delete(Session))
        await db.commit()
    print("Password reset; all sessions revoked")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("area", choices=["db", "admin", "backup", "doctor"])
    parser.add_argument("action", nargs="?", choices=["upgrade", "create", "reset-password"])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if (args.area, args.action) == ("db", "upgrade"):
        migrate()
    elif (args.area, args.action) == ("admin", "create"):
        asyncio.run(create_admin())
    elif (args.area, args.action) == ("admin", "reset-password"):
        asyncio.run(reset_password())
    elif args.area == "backup" and args.action is None and args.output:
        print(create_backup(get_settings(), args.output, args.env_file))
    elif args.area == "doctor" and args.action is None:
        print(json.dumps(doctor(get_settings()), indent=2))
    else:
        parser.error("Unsupported command")


if __name__ == "__main__":
    main()
