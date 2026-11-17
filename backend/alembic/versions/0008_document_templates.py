"""Add the document template library.

Revision ID: 0008_document_templates
Revises: 0007_drop_app_settings
Create Date: 2026-11-17

Standard templates are global and managed by administrators; user templates
belong to one account. The binary itself is copied into blob storage, so a
template survives deletion of the project file it was created from.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_document_templates"
down_revision = "0007_drop_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("extension", sa.String(length=16), nullable=False),
        sa.Column("mime", sa.String(length=255), nullable=True),
        sa.Column("storage_key", sa.String(length=64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "source_node_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=64), nullable=False, server_default="user"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_document_templates_owner_id", "document_templates", ["owner_id"])
    op.create_index("ix_document_templates_scope", "document_templates", ["scope"])
    op.create_index(
        "ix_document_templates_scope_owner", "document_templates", ["scope", "owner_id"]
    )
    op.create_index("ix_document_templates_name", "document_templates", ["name"])


def downgrade() -> None:
    op.drop_index("ix_document_templates_name", table_name="document_templates")
    op.drop_index("ix_document_templates_scope_owner", table_name="document_templates")
    op.drop_index("ix_document_templates_scope", table_name="document_templates")
    op.drop_index("ix_document_templates_owner_id", table_name="document_templates")
    op.drop_table("document_templates")


