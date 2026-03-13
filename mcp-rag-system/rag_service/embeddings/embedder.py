"""
Embedding backend factory.

Selects the embedding implementation based on the EMBEDDING_BACKEND env var:

  EMBEDDING_BACKEND=sentence_transformers  (default — local model, no API key)
  EMBEDDING_BACKEND=vertex_ai              (GCP Cloud Run — requires Vertex AI API)

Both backends expose the same interface:
  get_embedding_dimension() -> int
  embed_texts(texts)        -> list[list[float]]
  embed_query(query)        -> list[float]

Local sentence-transformers model is controlled by EMBEDDING_MODEL env var:
  default: all-MiniLM-L6-v2  (384 dims, fast)

Vertex AI model is controlled by VERTEX_AI_EMBEDDING_MODEL env var:
  default: text-embedding-005  (768 dims)

IMPORTANT: If you switch backends, the Qdrant collection dimension changes.
You must delete and recreate the collection (or use a separate collection name).
"""

from __future__ import annotations

import os
from functools import lru_cache

_BACKEND = os.getenv("EMBEDDING_BACKEND", "sentence_transformers").lower()

# -------------------------------------------------------------------
# sentence-transformers backend (default)
# -------------------------------------------------------------------
_MODEL_NAME  = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
_MODEL_CACHE = os.getenv("MODEL_CACHE_DIR", "/app/models")


@lru_cache(maxsize=1)
def _load_st_model():
    from sentence_transformers import SentenceTransformer
    print(f"[Embedder] Loading sentence-transformers model '{_MODEL_NAME}' ...")
    model = SentenceTransformer(_MODEL_NAME, cache_folder=_MODEL_CACHE)
    print(f"[Embedder] Model loaded. Dimension: {model.get_sentence_embedding_dimension()}")
    return model


def _st_get_dimension() -> int:
    return _load_st_model().get_sentence_embedding_dimension()


def _st_embed_texts(texts: list[str]) -> list[list[float]]:
    model = _load_st_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False).tolist()


def _st_embed_query(query: str) -> list[float]:
    return _st_embed_texts([query])[0]


# -------------------------------------------------------------------
# Vertex AI backend
# -------------------------------------------------------------------

def _vx_get_dimension() -> int:
    from .vertex_embedder import get_embedding_dimension
    return get_embedding_dimension()


def _vx_embed_texts(texts: list[str]) -> list[list[float]]:
    from .vertex_embedder import embed_texts
    return embed_texts(texts)


def _vx_embed_query(query: str) -> list[float]:
    from .vertex_embedder import embed_query
    return embed_query(query)


# -------------------------------------------------------------------
# Public API — delegates to selected backend
# -------------------------------------------------------------------

if _BACKEND == "vertex_ai":
    print("[Embedder] Backend: Vertex AI")
    get_embedding_dimension = _vx_get_dimension
    embed_texts             = _vx_embed_texts
    embed_query             = _vx_embed_query
else:
    if _BACKEND != "sentence_transformers":
        print(f"[Embedder] Unknown EMBEDDING_BACKEND='{_BACKEND}', falling back to sentence_transformers")
    print("[Embedder] Backend: sentence-transformers")
    get_embedding_dimension = _st_get_dimension
    embed_texts             = _st_embed_texts
    embed_query             = _st_embed_query
