"""Persistent general task queue and attempt records.

Revision ID: 0002_general_queue
Revises: 0001_foundation
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_general_queue"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("source_fingerprint", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(64)),
        sa.Column("pending_action", sa.String(16)),
        sa.Column("blocked_reason", sa.String(32)),
        sa.Column("download_subdir", sa.Text(), nullable=False),
        sa.Column("task_directory_key", sa.Text(), nullable=False, unique=True),
        sa.Column("selection_json", sa.Text()),
        sa.Column("options_json", sa.Text()),
        sa.Column("progress_json", sa.Text()),
        sa.Column("retry_cycle", sa.Integer()),
        sa.Column("attempt_count", sa.Integer()),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("warnings_json", sa.Text()),
        sa.Column("revision", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('general', 'video')", name="ck_task_kind"),
        sa.CheckConstraint("status IN ('queued','resolving','awaiting_selection','downloading','postprocessing','retry_wait','paused','stopped','completed','failed','cancelled')", name="ck_task_status"),
    )
    op.create_index("ix_tasks_source_fingerprint", "tasks", ["source_fingerprint"])
    op.create_index("ix_tasks_status_created", "tasks", ["status", "created_at"])
    op.create_index("ix_tasks_kind_status_updated", "tasks", ["kind", "status", "updated_at"])
    op.create_index("ix_tasks_finished_id", "tasks", ["finished_at", "id"])
    op.create_table(
        "work_queue", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("work_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, unique=True),
        sa.Column("available_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("work_type", "resource_id"),
    )
    op.create_table(
        "task_attempts", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("engine_ref", sa.String(128)),
        sa.Column("process_identity_json", sa.Text()),
        sa.Column("tool_version", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("task_id", "cycle", "ordinal"),
    )
    op.create_index("ix_task_attempts_task_id", "task_attempts", ["task_id"])
    op.create_index("ix_task_attempts_engine_ref", "task_attempts", ["engine_ref"])
    op.create_table(
        "aria2_bindings", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("task_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gid", sa.String(16), nullable=False, unique=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("last_engine_state", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_aria2_bindings_task_id", "aria2_bindings", ["task_id"])
    op.create_table(
        "files", sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("relative_path", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("mtime_ns", sa.Integer()),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("mime_type", sa.String(128)),
        sa.Column("availability", sa.String(16)),
        sa.Column("is_complete", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_files_task_id", "files", ["task_id"])
    op.create_table(
        "idempotency_keys", sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("idempotency_keys")
    op.drop_table("files")
    op.drop_table("aria2_bindings")
    op.drop_table("task_attempts")
    op.drop_table("work_queue")
    op.drop_table("tasks")
