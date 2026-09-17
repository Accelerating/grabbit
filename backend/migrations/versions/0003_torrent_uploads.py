"""Bounded private torrent uploads and parsed candidate summaries.

Revision ID: 0003_torrent_uploads
Revises: 0002_general_queue
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_torrent_uploads"
down_revision = "0002_general_queue"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "torrent_uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("blob_key", sa.String(255), nullable=False, unique=True),
        sa.Column("infohash", sa.String(40), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_torrent_uploads_infohash", "torrent_uploads", ["infohash"])
    op.create_index("ix_torrent_uploads_expires_at", "torrent_uploads", ["expires_at"])


def downgrade():
    op.drop_index("ix_torrent_uploads_expires_at", table_name="torrent_uploads")
    op.drop_index("ix_torrent_uploads_infohash", table_name="torrent_uploads")
    op.drop_table("torrent_uploads")
