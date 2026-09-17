"""Remove database foreign keys; references are validated by the application.

Revision ID: 0006_drop_foreign_keys
Revises: 0005_cookie_profiles
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_drop_foreign_keys"
down_revision = "0005_cookie_profiles"
branch_labels = None
depends_on = None

NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in inspector.get_table_names():
        foreign_keys = sa.inspect(bind).get_foreign_keys(table)
        if not foreign_keys:
            continue
        with op.batch_alter_table(table, recreate="always", naming_convention=NAMING_CONVENTION) as batch:
            for constraint in foreign_keys:
                name = constraint["name"] or (
                    f"fk_{table}_{constraint['constrained_columns'][0]}_{constraint['referred_table']}"
                )
                batch.drop_constraint(name, type_="foreignkey")


def downgrade():
    raise NotImplementedError("Reintroducing database foreign keys is not supported")
