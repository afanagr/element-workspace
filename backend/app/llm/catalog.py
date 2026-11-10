"""Static provider metadata: labels, default models, env var names, base URLs."""

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
    api_style: str = "openai-compatible"  # openai | anthropic | gemini | openai-compatible


PROVIDERS: dict[str, ProviderMeta] = {
    "openai": ProviderMeta(
        name="openai",
        label="OpenAI",
        env_key="OPENAI_API_KEY",
        default_model="gpt-4o",
        models=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini", "o3-mini"),
        base_url=None,
        supports_embeddings=True,
        api_style="openai",
    ),
    "anthropic": ProviderMeta(
        name="anthropic",
        label="Anthropic",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-20250514",
        models=(
            "claude-sonnet-4-20250514",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ),
        base_url="https://api.anthropic.com",
        supports_embeddings=False,
        api_style="anthropic",
    ),
    "gemini": ProviderMeta(
        name="gemini",
        label="Google Gemini",
        env_key="GOOGLE_API_KEY",
        default_model="gemini-2.0-flash",
        models=("gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro", "gemini-1.5-flash"),
        base_url=None,
        supports_embeddings=True,
        api_style="gemini",
    ),
    "deepseek": ProviderMeta(
        name="deepseek",
        label="DeepSeek",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-flash",
        models=("deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"),
        base_url="https://api.deepseek.com",
        supports_vision=False,
        supports_embeddings=False,
        api_style="openai-compatible",
    ),
    "openrouter": ProviderMeta(
        name="openrouter",
        label="OpenRouter",
        env_key="OPENROUTER_API_KEY",
        default_model="openai/gpt-4o-mini",
        models=(
            "openai/gpt-4o-mini",
            "openai/gpt-4o",
            "anthropic/claude-3.5-sonnet",
            "google/gemini-flash-1.5",
            "meta-llama/llama-3.3-70b-instruct",
            "deepseek/deepseek-chat",
        ),
        base_url="https://openrouter.ai/api/v1",
        supports_embeddings=False,
        api_style="openai-compatible",
    ),
    "ollama": ProviderMeta(
        name="ollama",
        label="Ollama (local)",
        env_key="OLLAMA_API_KEY",
        default_model="llama3.2",
        models=("llama3.2", "llama3.1", "qwen2.5", "mistral", "phi3.5", "gemma2"),
        base_url="http://ollama:11434/v1",
        supports_vision=False,
        supports_embeddings=True,
        local=True,
        api_style="openai-compatible",
    ),
    "groq": ProviderMeta(
        name="groq",
        label="Groq",
        env_key="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"),
        base_url="https://api.groq.com/openai/v1",
        supports_vision=False,
        api_style="openai-compatible",
    ),
    "together": ProviderMeta(
        name="together",
        label="Together AI",
        env_key="TOGETHER_API_KEY",
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        models=(
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ),
        base_url="https://api.together.xyz/v1",
        supports_vision=False,
        api_style="openai-compatible",
    ),
    "xai": ProviderMeta(
        name="xai",
        label="xAI Grok",
        env_key="XAI_API_KEY",
        default_model="grok-2-latest",
        models=("grok-2-latest", "grok-2-vision-latest", "grok-beta"),
        base_url="https://api.x.ai/v1",
        supports_vision=True,
        api_style="openai-compatible",
    ),
}

# Order used when the requested provider is unavailable.
FALLBACK_ORDER: tuple[str, ...] = (
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "openrouter",
    "groq",
    "together",
    "xai",
    "ollama",
)

EMBEDDING_PROVIDERS: tuple[str, ...] = ("openai", "gemini", "ollama", "local")

EMBEDDING_MODELS: dict[str, tuple[str, ...]] = {
    "openai": ("text-embedding-3-small", "text-embedding-3-large"),
    "gemini": ("text-embedding-004", "gemini-embedding-001"),
    "ollama": ("nomic-embed-text", "mxbai-embed-large"),
    "local": ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",),
}

EMBEDDING_DIMENSIONS: dict[str, int] = {
    # Used only as a default before the first real embedding is computed.
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-004": 768,
    "gemini-embedding-001": 768,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
}
