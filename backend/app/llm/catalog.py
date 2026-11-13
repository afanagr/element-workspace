"""Static provider metadata.

Three chat backends ship with the workspace, all speaking the OpenAI
chat-completions dialect:

* ``deepseek``   - DeepSeek cloud API (``https://api.deepseek.com``).
* ``qwen``       - Alibaba Cloud DashScope in OpenAI-compatible mode (cloud).
* ``qwen-local`` - any OpenAI-compatible server hosting Qwen weights locally
  (Ollama, vLLM, LM Studio, ...). It needs no API key; the endpoint comes from
  ``QWEN_LOCAL_BASE_URL`` and an empty value disables the provider.

Embeddings are always computed locally (sentence-transformers) because none of
the curated cloud providers is used for vectorisation.
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
    supports_vision: bool = False
    supports_embeddings: bool = False
    local: bool = False
    requires_api_key: bool = True
    api_style: str = "openai-compatible"  # openai | openai-compatible


DEEPSEEK_MODELS: tuple[str, ...] = ("deepseek-chat", "deepseek-reasoner")

# Curated DashScope catalogue. The live ``/models`` listing is merged in when
# the endpoint exposes it, so newly released Qwen models appear without a code
# change.
QWEN_MODELS: tuple[str, ...] = (
    "qwen-plus",
    "qwen-max",
    "qwen-turbo",
    "qwen-long",
    "qwen3-max",
    "qwen3-235b-a22b",
    "qwq-plus",
    "qwen2.5-72b-instruct",
)

# Ollama/vLLM tag names. Anything the local server reports is merged in when its
# model list is reachable.
QWEN_LOCAL_MODELS: tuple[str, ...] = (
    "qwen2.5:7b-instruct",
    "qwen2.5:14b-instruct",
    "qwen2.5:32b-instruct",
    "qwen2.5-coder:7b-instruct",
    "qwen3:8b",
    "qwen3:14b",
)

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
    "qwen": ProviderMeta(
        name="qwen",
        label="Qwen (DashScope)",
        env_key="QWEN_API_KEY",
        default_model="qwen-plus",
        models=QWEN_MODELS,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        supports_vision=False,
        supports_embeddings=False,
        api_style="openai-compatible",
    ),
    "qwen-local": ProviderMeta(
        name="qwen-local",
        label="Qwen (local)",
        env_key="QWEN_LOCAL_API_KEY",
        default_model="qwen2.5:7b-instruct",
        models=QWEN_LOCAL_MODELS,
        base_url=None,  # taken from QWEN_LOCAL_BASE_URL
        supports_vision=False,
        supports_embeddings=False,
        local=True,
        requires_api_key=False,
        api_style="openai-compatible",
    ),
}

# Order used when the requested provider is unavailable.
FALLBACK_ORDER: tuple[str, ...] = ("deepseek", "qwen", "qwen-local")

# Providers whose credentials are issued per account by an administrator.
CLOUD_PROVIDERS: tuple[str, ...] = tuple(
    name for name, meta in PROVIDERS.items() if not meta.local
)

# Local embeddings are the only way to vectorise documents.
EMBEDDING_PROVIDERS: tuple[str, ...] = ("local",)

EMBEDDING_MODELS: dict[str, tuple[str, ...]] = {
    "local": ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",),
}

EMBEDDING_DIMENSIONS: dict[str, int] = {
    # Used only as a default before the first real embedding is computed.
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
}
