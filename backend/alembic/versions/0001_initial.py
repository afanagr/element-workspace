"""Initial schema: file tree, versions, chats, operations, credentials, logs.

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01

The initial revision materialises ``Base.metadata`` so the ORM models stay the
single source of truth. Later revisions should use explicit ``op`` calls.
"""

from __future__ import annotations

from alembic import op

from app.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
