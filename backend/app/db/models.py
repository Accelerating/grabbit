from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def uuid_text() -> str:
    return str(uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    sessions: Mapped[list["Session"]] = relationship(
        primaryjoin=lambda: User.id == foreign(Session.user_id),
        back_populates="user", cascade="all, delete-orphan",
    )


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    user: Mapped[User] = relationship(
        primaryjoin=lambda: foreign(Session.user_id) == User.id, back_populates="sessions"
    )


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RuntimeControl(Base):
    __tablename__ = "runtime_control"
    __table_args__ = (CheckConstraint("id = 1", name="ck_runtime_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    dispatch_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DependencyInstallJob(Base):
    __tablename__ = "dependency_install_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="queued")
    log_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("kind IN ('general', 'video')", name="ck_task_kind"),
        CheckConstraint("status IN ('queued','resolving','awaiting_selection','downloading','postprocessing','retry_wait','paused','stopped','completed','failed','cancelled')", name="ck_task_status"),
        Index("ix_tasks_status_created", "status", "created_at"),
        Index("ix_tasks_kind_status_updated", "kind", "status", "updated_at"),
        Index("ix_tasks_finished_id", "finished_at", "id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    phase: Mapped[str | None] = mapped_column(String(64))
    pending_action: Mapped[str | None] = mapped_column(String(16))
    blocked_reason: Mapped[str | None] = mapped_column(String(32))
    download_subdir: Mapped[str] = mapped_column(Text, nullable=False)
    task_directory_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    group_id: Mapped[str | None] = mapped_column(String(36), index=True)
    cookie_profile_id: Mapped[str | None] = mapped_column(String(36), index=True)
    cookie_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    selection_json: Mapped[str] = mapped_column(Text, default="{}")
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    retry_cycle: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkItem(Base):
    __tablename__ = "work_queue"
    __table_args__ = (UniqueConstraint("work_type", "resource_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    work_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    position: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VideoAnalysisJob(Base):
    __tablename__ = "video_analysis_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    site_key: Mapped[str] = mapped_column(String(64), nullable=False)
    cookie_profile_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    page_start: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_index: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class VideoAnalysisItem(Base):
    __tablename__ = "video_analysis_items"
    __table_args__ = (UniqueConstraint("analysis_job_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    analysis_job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskGroup(Base):
    __tablename__ = "task_groups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    site_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("task_id", "cycle", "ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    cycle: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    engine_ref: Mapped[str | None] = mapped_column(String(128), index=True)
    process_identity_json: Mapped[str | None] = mapped_column(Text)
    tool_version: Mapped[str | None] = mapped_column(String(64))
    cookie_version: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))


class Aria2Binding(Base):
    __tablename__ = "aria2_bindings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_id: Mapped[str] = mapped_column(String(36))
    gid: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    last_engine_state: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FileRecord(Base):
    __tablename__ = "files"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    task_id: Mapped[str | None] = mapped_column(String(36), index=True)
    relative_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    mtime_ns: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="other")
    mime_type: Mapped[str | None] = mapped_column(String(128))
    availability: Mapped[str] = mapped_column(String(16), default="present")
    is_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    probe_status: Mapped[str] = mapped_column(String(16), default="unprobed")
    media_json: Mapped[str | None] = mapped_column(Text)
    media_mtime_ns: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class FileOperation(Base):
    __tablename__ = "file_operations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    target_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    results_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TorrentUpload(Base):
    """A bounded, private torrent upload awaiting task creation.

    The original blob is deliberately kept outside the database.  ``blob_key``
    is an application-controlled relative key under the private state root;
    it is never returned as a filesystem path to clients.
    """

    __tablename__ = "torrent_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    blob_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    infohash: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)


class TaskSource(Base):
    __tablename__ = "task_sources"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    torrent_blob_key: Mapped[str | None] = mapped_column(String(255))
    magnet_infohash: Mapped[str | None] = mapped_column(String(40))
    source_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")


class CookieProfile(Base):
    __tablename__ = "cookie_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_text)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    site_key: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    domain_scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    secret_file_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_result_code: Mapped[str | None] = mapped_column(String(64))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SiteCookieDefault(Base):
    __tablename__ = "site_cookie_defaults"
    site_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cookie_profile_id: Mapped[str | None] = mapped_column(String(36))
