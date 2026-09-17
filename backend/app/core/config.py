from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GRABBIT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./grabbit.db"
    download_root: Path = Path("./downloads")
    private_root: Path = Path("./private")
    public_origin: str = "http://localhost:8000"
    secure_cookies: bool = False
    session_days: int = Field(default=7, ge=1, le=30)
    static_dir: Path = Path(__file__).resolve().parents[1] / "static"

    @field_validator("download_root", "private_root", "static_dir", mode="after")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("public_origin")
    @classmethod
    def normalize_origin(cls, value: str) -> str:
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
