"""Durable file operations; references remain application-managed."""
from alembic import op
import sqlalchemy as sa

revision = "0008_file_operations"
down_revision = "0007_video_analysis_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "file_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("preview_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("target_snapshot_json", sa.Text(), nullable=False),
        sa.Column("results_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_file_operations_status", "file_operations", ["status"])


def downgrade():
    op.drop_index("ix_file_operations_status", table_name="file_operations")
    op.drop_table("file_operations")
