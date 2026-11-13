"""Per-account model connections (replaces the built-in provider catalogue).

Revision ID: 0005_model_connections
Revises: 0004_provider_endpoints
Create Date: 2026-11-13

On a fresh database revision 0001 already materialises ``Base.metadata`` (so the
table exists and this migration only copies legacy rows); an existing
installation gets the table created here. The legacy ``users.deepseek_*`` /
``qwen_*`` / ``provider_base_urls`` values are copied into ``model_connections``
so nobody loses a working key during the switch.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import sqlalchemy as sa
from alembic import op

from app.config import settings
from app.models import Base

revision = "0005_model_connections"
down_revision = "0004_provider_endpoints"
branch_labels = None
depends_on = None

DEFAULT_QWEN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _create_table(name: str, tables: set[str]) -> None:
    if name not in tables and name in Base.metadata.tables:
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def _overrides(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _url(overrides: dict[str, Any], key: str, default: str) -> str:
    value = overrides.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return (default or "").strip()


def _legacy_candidates(row: Any, columns: set[str]) -> list[dict[str, Any]]:
    overrides = _overrides(row["provider_base_urls"]) if "provider_base_urls" in columns else {}
    deepseek_key = row.get("deepseek_api_key_enc")
    deepseek_model = (row.get("deepseek_model") or "").strip()
    qwen_key = row.get("qwen_api_key_enc")
    qwen_model = (row.get("qwen_model") or "").strip()
    local_key = row.get("qwen_local_api_key_enc")
    local_model = (row.get("qwen_local_model") or "").strip()

    candidates: list[dict[str, Any]] = []
    if deepseek_key or deepseek_model:
        candidates.append(
            {
                "name": "DeepSeek",
                "base_url": _url(overrides, "deepseek", settings.deepseek_base_url),
                "api_key_enc": deepseek_key,
                "model": deepseek_model or settings.llm_model or "deepseek-chat",
            }
        )
    if qwen_key or qwen_model:
        candidates.append(
            {
                "name": "Qwen",
                "base_url": _url(overrides, "qwen", settings.qwen_base_url)
                or DEFAULT_QWEN_URL,
                "api_key_enc": qwen_key,
                "model": qwen_model or settings.qwen_model or "qwen-plus",
            }
        )
    if local_key or local_model:
        local_url = _url(overrides, "qwen-local", settings.qwen_local_base_url)
        if local_url:
            candidates.append(
                {
                    "name": "Qwen (local)",
                    "base_url": local_url,
                    "api_key_enc": local_key,
                    "model": local_model or settings.qwen_local_model or "qwen2.5:7b-instruct",
                }
            )
    return candidates


def upgrade() -> None:
    tables = _tables()
    _create_table("model_connections", tables)
    if "model_connections" not in _tables() or "users" not in tables:
        return

    bind = op.get_bind()
    user_columns = _columns("users")
    if "deepseek_api_key_enc" not in user_columns:
        return

    selected = [
        "id",
        "deepseek_api_key_enc",
        "deepseek_model",
        "qwen_api_key_enc",
        "qwen_model",
        "qwen_local_api_key_enc",
        "qwen_local_model",
    ]
    if "provider_base_urls" in user_columns:
        selected.append("provider_base_urls")

    rows = (
        bind.execute(sa.text(f"SELECT {', '.join(selected)} FROM users")).mappings().all()
    )
    existing = {
        (str(row[0]), str(row[1]).lower())
        for row in bind.execute(sa.text("SELECT user_id, name FROM model_connections")).all()
    }

    insert = sa.text(
        "INSERT INTO model_connections "
        "(id, user_id, name, base_url, api_key_enc, model, supports_tools, created_at, updated_at) "
        "VALUES (:id, :user_id, :name, :base_url, :api_key_enc, :model, true, now(), now())"
    )
    for row in rows:
        for item in _legacy_candidates(row, user_columns):
            key = (str(row["id"]), item["name"].lower())
            if key in existing:
                continue
            bind.execute(
                insert,
                {
                    "id": uuid.uuid4(),
                    "user_id": row["id"],
                    "name": item["name"],
                    "base_url": item["base_url"][:500],
                    "api_key_enc": item["api_key_enc"],
                    "model": item["model"][:128],
                },
            )
            existing.add(key)


def downgrade() -> None:
    if "model_connections" in _tables():
        op.drop_table("model_connections")
