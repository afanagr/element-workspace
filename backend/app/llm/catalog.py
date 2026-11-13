"""Embedding metadata.

Chat endpoints are not hard-coded any more: every account owns a list of
OpenAI-compatible connections (``model_connections``) created by an
administrator. Embeddings are always computed locally with
sentence-transformers.
"""

from __future__ import annotations

EMBEDDING_PROVIDERS: tuple[str, ...] = ("local",)

EMBEDDING_MODELS: dict[str, tuple[str, ...]] = {
    "local": ("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",),
}

EMBEDDING_DIMENSIONS: dict[str, int] = {
    # Used only as a default before the first real embedding is computed.
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
}
