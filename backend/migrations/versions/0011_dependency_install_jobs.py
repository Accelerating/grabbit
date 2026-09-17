"""Restricted package installation jobs; no foreign keys."""
from alembic import op
import sqlalchemy as sa

revision = "0011_dependency_install_jobs"
down_revision = "0010_video_collections"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dependency_install_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("log_text", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_dependency_install_jobs_status", "dependency_install_jobs", ["status"])


def downgrade():
    op.drop_index("ix_dependency_install_jobs_status", table_name="dependency_install_jobs")
    op.drop_table("dependency_install_jobs")
