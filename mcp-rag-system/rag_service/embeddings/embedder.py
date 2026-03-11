"""
Embedding generation using sentence-transformers (local, no API key).

The model is loaded once at startup and cached in memory.
Default: all-MiniLM-L6-v2  (384 dimensions, fast, good quality)

Alternative models (set EMBEDDING_MODEL env var):
  - all-mpnet-base-v2          (768 dims, higher quality, slower)
  - paraphrase-multilingual-MiniLM-L12-v2  (384 dims, multilingual)
  - BAAI/bge-small-en-v1.5     (384 dims, strong retrieval performance)
"""

from __future__ import annotations

import os
from functools import lru_cache

from sentence_transformers import SentenceTransformer

_MODEL_NAME = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
_MODEL_CACHE = os.getenv("MODEL_CACHE_DIR", "/app/models")


@lru_cache(maxsize=1)
def _load_model() -> SentenceTransformer:
    print(f"[Embedder] Loading model '{_MODEL_NAME}' ...")
    model = SentenceTransformer(_MODEL_NAME, cache_folder=_MODEL_CACHE)
    print(f"[Embedder] Model loaded. Dimension: {model.get_sentence_embedding_dimension()}")
    return model


def get_embedding_dimension() -> int:
    return _load_model().get_sentence_embedding_dimension()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return a list of embedding vectors for the given texts."""
    model = _load_model()
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    """Return a single embedding vector for a search query."""
    return embed_texts([query])[0]
