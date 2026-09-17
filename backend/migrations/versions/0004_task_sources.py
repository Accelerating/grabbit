"""Persist source details needed to resume torrent tasks.

Revision ID: 0004_task_sources
Revises: 0003_torrent_uploads
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_task_sources"
down_revision = "0003_torrent_uploads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_sources",
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("torrent_blob_key", sa.String(255)),
        sa.Column("magnet_infohash", sa.String(40)),
        sa.Column("source_snapshot_json", sa.Text()),
    )


def downgrade():
    op.drop_table("task_sources")
