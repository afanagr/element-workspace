"""Drop the legacy per-provider credential columns from ``users``.

Revision ID: 0006_drop_legacy_provider_columns
Revises: 0005_model_connections
Create Date: 2026-11-16

Releases before v2 kept DeepSeek/Qwen credentials and endpoint overrides
directly on the account row. ``0005_model_connections`` copied them into
``model_connections``; this revision removes the now unused columns so the
schema matches ``app.models`` again (no encrypted leftovers in ``users``).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_drop_legacy_provider_columns"
down_revision = "0005_model_connections"
branch_labels = None
depends_on = None

# name -> type used to recreate the column on downgrade
LEGACY_COLUMNS: dict[str, sa.types.TypeEngine] = {
    "deepseek_api_key_enc": sa.Text(),
    "deepseek_model": sa.String(length=64),
    "qwen_api_key_enc": sa.Text(),
    "qwen_model": sa.String(length=64),
    "qwen_local_api_key_enc": sa.Text(),
    "qwen_local_model": sa.String(length=64),
    "provider_base_urls": postgresql.JSONB(),
}


def _user_columns() -> set[str] | None:
    """Column names of ``users``; ``None`` in offline (``--sql``) mode."""
    context = op.get_context()
    if context.as_sql:
        return None
    inspector = sa.inspect(op.get_bind())
    if "users" not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns("users")}


def upgrade() -> None:
    existing = _user_columns()
    for name in LEGACY_COLUMNS:
        if existing is None or name in existing:
            op.drop_column("users", name)


def downgrade() -> None:
    # The copied credentials cannot be restored, only the table shape.
    existing = _user_columns()
    for name, column_type in LEGACY_COLUMNS.items():
        if existing is None or name not in existing:
            op.add_column("users", sa.Column(name, column_type, nullable=True))
