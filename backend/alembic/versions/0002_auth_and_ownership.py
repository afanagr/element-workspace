"""Users, bearer tokens, data ownership and the removal of provider credentials.

Revision ID: 0002_auth_and_ownership
Revises: 0001_initial
Create Date: 2026-02-01

The revision is written defensively: revision 0001 materialises the *current*
``Base.metadata``, so on a fresh database the objects below already exist and the
migration becomes a no-op, while an existing single-user installation gets the
new columns and tables added in place.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.models import Base

revision = "0002_auth_and_ownership"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _create_table(name: str, tables: set[str]) -> None:
    if name not in tables and name in Base.metadata.tables:
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()

    _create_table("users", tables)
    _create_table("auth_tokens", tables)

    if "owner_id" not in _columns("nodes"):
        op.add_column("nodes", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_nodes_owner_id_users", "nodes", "users", ["owner_id"], ["id"], ondelete="CASCADE"
        )
        op.create_index("ix_nodes_owner_id", "nodes", ["owner_id"])
        op.create_index("ix_nodes_owner_kind", "nodes", ["owner_id", "kind"])

    if "owner_id" not in _columns("chat_sessions"):
        op.add_column(
            "chat_sessions", sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            "fk_chat_sessions_owner_id_users",
            "chat_sessions",
            "users",
            ["owner_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index("ix_chat_sessions_owner_id", "chat_sessions", ["owner_id"])

    # Provider credentials are gone: DeepSeek keys now live on the user row.
    if "provider_credentials" in tables:
        op.drop_table("provider_credentials")


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    if "provider_credentials" not in tables and "provider_credentials" in Base.metadata.tables:
        pass  # the table was dropped on purpose; nothing to restore here

    for table, names in (
        ("chat_sessions", ("ix_chat_sessions_owner_id", "fk_chat_sessions_owner_id_users")),
        ("nodes", ("ix_nodes_owner_kind", "ix_nodes_owner_id", "fk_nodes_owner_id_users")),
    ):
        if table not in tables:
            continue
        for name in names:
            try:
                if name.startswith("ix_"):
                    op.drop_index(name, table_name=table)
                else:
                    op.drop_constraint(name, table, type_="foreignkey")
            except Exception:  # noqa: BLE001 - downgrade is best effort
                pass
        columns = _columns(table)
        if "owner_id" in columns:
            op.drop_column(table, "owner_id")

    tables = _tables()
    if "auth_tokens" in tables:
        op.drop_table("auth_tokens")
    if "users" in tables:
        op.drop_table("users")
