"""Static provider metadata.

The workspace ships with exactly one chat provider - DeepSeek - which speaks the
OpenAI chat-completions dialect and supports tool calling. Embeddings are always
computed locally (sentence-transformers) because DeepSeek has no embeddings API.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderMeta:
    name: str
    label: str
    env_key: str
    default_model: str
    models: tuple[str, ...]
    base_url: str | None
    supports_tools: bool = True
    supports_vision: bool = True
    supports_embeddings: bool = False
    local: bool = False
    api_style: str = "openai-compatible"  # openai | openai-compatible


DEEPSEEK_MODELS: tuple[str, ...] = ("deepseek-chat", "deepseek-reasoner")

PROVIDERS: dict[str, ProviderMeta] = {
    "deepseek": ProviderMeta(
        name="deepseek",
        label="DeepSeek",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        models=DEEPSEEK_MODELS,
        base_url="https://api.deepseek.com",
        supports_vision=False,
        supports_embeddings=False,
        api_style="openai-compatible",
    ),
}

# Order used when the requested provider is unavailable - only DeepSeek exists.
FALLBACK_ORDER: tuple[str, ...] = ("deepseek",)

# Local embeddings are the only way to vectorise documents.
EMBEDDING_PROVIDERS: tuple[str, ...] = ("local",)

EMBEDDING_MODELS: dict[str, tuple[str, ...]] = {
    "local": ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",),
}

EMBEDDING_DIMENSIONS: dict[str, int] = {
    # Used only as a default before the first real embedding is computed.
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
}
