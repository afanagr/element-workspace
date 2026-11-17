"""Drop the unused ``app_settings`` table.

Revision ID: 0007_drop_app_settings
Revises: 0006_drop_provider_columns
Create Date: 2026-11-17

``AppSetting`` was never read or written: every setting lives in the
environment or in a dedicated table. The model is gone, so fresh installations
no longer create the table and existing ones get rid of it here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_drop_app_settings"
down_revision = "0006_drop_provider_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    context = op.get_context()
    if context.as_sql:
        op.drop_table("app_settings")
        return
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "app_settings" in tables:
        op.drop_table("app_settings")


def downgrade() -> None:
    # Only the shape comes back; the table never held data.
    context = op.get_context()
    if context.as_sql:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        return
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "app_settings" not in tables:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(length=64), primary_key=True),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
