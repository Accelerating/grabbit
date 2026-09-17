"""Private cookie profile metadata and video task references.

Revision ID: 0005_cookie_profiles
Revises: 0004_task_sources
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_cookie_profiles"
down_revision = "0004_task_sources"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("cookie_profiles"):
        op.create_table(
        "cookie_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("site_key", sa.String(64), nullable=False),
        sa.Column("domain_scope_json", sa.Text(), nullable=False),
        sa.Column("secret_file_key", sa.String(255), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("last_result_code", sa.String(64)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
    if "ix_cookie_profiles_site_key" not in {item["name"] for item in sa.inspect(bind).get_indexes("cookie_profiles")}:
        op.create_index("ix_cookie_profiles_site_key", "cookie_profiles", ["site_key"])
    if not sa.inspect(bind).has_table("site_cookie_defaults"):
        op.create_table(
            "site_cookie_defaults",
            sa.Column("site_key", sa.String(64), primary_key=True),
            sa.Column("cookie_profile_id", sa.String(36)),
        )
    task_columns = {item["name"] for item in sa.inspect(bind).get_columns("tasks")}
    if "cookie_profile_id" not in task_columns:
        op.add_column("tasks", sa.Column("cookie_profile_id", sa.String(36)))
    if "cookie_name_snapshot" not in task_columns:
        op.add_column("tasks", sa.Column("cookie_name_snapshot", sa.String(255)))
    if "ix_tasks_cookie_profile_id" not in {item["name"] for item in sa.inspect(bind).get_indexes("tasks")}:
        op.create_index("ix_tasks_cookie_profile_id", "tasks", ["cookie_profile_id"])
    if "cookie_version" not in {item["name"] for item in sa.inspect(bind).get_columns("task_attempts")}:
        op.add_column("task_attempts", sa.Column("cookie_version", sa.Integer()))


def downgrade():
    op.drop_column("task_attempts", "cookie_version")
    op.drop_index("ix_tasks_cookie_profile_id", table_name="tasks")
    op.drop_column("tasks", "cookie_name_snapshot")
    op.drop_column("tasks", "cookie_profile_id")
    op.drop_table("site_cookie_defaults")
    op.drop_index("ix_cookie_profiles_site_key", table_name="cookie_profiles")
    op.drop_table("cookie_profiles")
