"""Durable video analysis jobs without database foreign keys."""
from alembic import op
import sqlalchemy as sa

revision = "0007_video_analysis_jobs"
down_revision = "0006_drop_foreign_keys"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "video_analysis_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("site_key", sa.String(64), nullable=False),
        sa.Column("cookie_profile_id", sa.String(36)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_video_analysis_jobs_cookie_profile_id", "video_analysis_jobs", ["cookie_profile_id"])
    op.create_index("ix_video_analysis_jobs_status", "video_analysis_jobs", ["status"])


def downgrade():
    op.drop_index("ix_video_analysis_jobs_status", table_name="video_analysis_jobs")
    op.drop_index("ix_video_analysis_jobs_cookie_profile_id", table_name="video_analysis_jobs")
    op.drop_table("video_analysis_jobs")
