"""Bounded collection item summaries; no database foreign keys."""
from alembic import op
import sqlalchemy as sa

revision = "0010_video_collections"
down_revision = "0009_media_probe"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("group_id", sa.String(36)))
        batch.create_index("ix_tasks_group_id", ["group_id"])
    with op.batch_alter_table("video_analysis_jobs") as batch:
        batch.add_column(sa.Column("page_start", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("next_index", sa.Integer()))
    op.create_table(
        "video_analysis_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("analysis_job_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("analysis_job_id", "ordinal"),
    )
    op.create_index("ix_video_analysis_items_analysis_job_id", "video_analysis_items", ["analysis_job_id"])
    op.create_table(
        "task_groups",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("site_key", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("task_groups")
    op.drop_index("ix_video_analysis_items_analysis_job_id", table_name="video_analysis_items")
    op.drop_table("video_analysis_items")
    with op.batch_alter_table("video_analysis_jobs") as batch:
        batch.drop_column("next_index")
        batch.drop_column("page_start")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_group_id")
        batch.drop_column("group_id")
