"""Per-user Qwen credentials.

Revision ID: 0003_qwen_provider
Revises: 0002_auth_and_ownership
Create Date: 2026-11-13

Revision 0001 materialises the current ``Base.metadata``, so on a fresh database
the columns below already exist and this migration is a no-op, while an existing
installation gets them added in place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_qwen_provider"
down_revision = "0002_auth_and_ownership"
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
    if "qwen_api_key_enc" not in columns:
        op.add_column("users", sa.Column("qwen_api_key_enc", sa.Text(), nullable=True))
    if "qwen_model" not in columns:
        op.add_column("users", sa.Column("qwen_model", sa.String(length=64), nullable=True))


def downgrade() -> None:
    if "users" not in _tables():
        return
    columns = _columns("users")
    for name in ("qwen_model", "qwen_api_key_enc"):
        if name in columns:
            op.drop_column("users", name)
