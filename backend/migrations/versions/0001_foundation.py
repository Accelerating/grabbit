"""Initial authentication and settings schema.

Revision ID: 0001_foundation
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("users", sa.Column("id", sa.String(36), primary_key=True), sa.Column("username", sa.String(64), nullable=False, unique=True), sa.Column("password_hash", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.create_table("sessions", sa.Column("id", sa.String(36), primary_key=True), sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False), sa.Column("token_hash", sa.String(64), nullable=False), sa.Column("csrf_token_hash", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table("settings", sa.Column("key", sa.String(64), primary_key=True), sa.Column("value_json", sa.Text(), nullable=False), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.create_table("runtime_control", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("dispatch_suspended", sa.Boolean(), nullable=False), sa.Column("blocked_reason", sa.String(32)), sa.Column("revision", sa.Integer(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True)), sa.CheckConstraint("id = 1", name="ck_runtime_singleton"))


def downgrade():
    op.drop_table("runtime_control")
    op.drop_table("settings")
    op.drop_table("sessions")
    op.drop_table("users")
