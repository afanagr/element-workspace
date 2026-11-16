"""Environment defaults used by the data migration of the old providers.

Nothing in the running application reads these values: chat models are
configured per account in ``/admin`` (table ``model_connections``). They exist
only so ``alembic/versions/0005_model_connections.py`` can fill in a sensible
endpoint and model when it copies a legacy ``users.deepseek_*`` / ``qwen_*`` row
whose URL or model column was empty.

The corresponding variables may be deleted from ``.env`` as soon as the upgrade
to v2 has been applied.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class MigrationDefaults(BaseSettings):
    """Legacy provider endpoints, read only while a migration runs."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    llm_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    qwen_local_base_url: str = ""
    qwen_local_model: str = "qwen2.5:7b-instruct"


migration_defaults = MigrationDefaults()
