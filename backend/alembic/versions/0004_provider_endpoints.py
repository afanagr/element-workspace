"""Per-account provider endpoints and local Qwen credentials.

Revision ID: 0004_provider_endpoints
Revises: 0003_qwen_provider
Create Date: 2026-11-13

Revision 0001 materialises the current ``Base.metadata``, so on a fresh database
the columns already exist and this migration is a no-op, while an existing
installation gets them added in place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_provider_endpoints"
down_revision = "0003_qwen_provider"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "users" not in _tables():
        return
    columns = _columns("users")
    if "qwen_local_api_key_enc" not in columns:
        op.add_column("users", sa.Column("qwen_local_api_key_enc", sa.Text(), nullable=True))
    if "qwen_local_model" not in columns:
        op.add_column("users", sa.Column("qwen_local_model", sa.String(length=64), nullable=True))
    if "provider_base_urls" not in columns:
        op.add_column(
            "users", sa.Column("provider_base_urls", postgresql.JSONB(), nullable=True)
        )


def downgrade() -> None:
    if "users" not in _tables():
        return
    columns = _columns("users")
    for name in ("provider_base_urls", "qwen_local_model", "qwen_local_api_key_enc"):
        if name in columns:
            op.drop_column("users", name)
