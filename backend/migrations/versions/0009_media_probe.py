"""Cached media metadata, without foreign-key constraints."""
from alembic import op
import sqlalchemy as sa

revision = "0009_media_probe"
down_revision = "0008_file_operations"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("files") as batch:
        batch.add_column(sa.Column("probe_status", sa.String(16), nullable=False, server_default="unprobed"))
        batch.add_column(sa.Column("media_json", sa.Text()))
        batch.add_column(sa.Column("media_mtime_ns", sa.Integer()))


def downgrade():
    with op.batch_alter_table("files") as batch:
        batch.drop_column("media_mtime_ns")
        batch.drop_column("media_json")
        batch.drop_column("probe_status")
