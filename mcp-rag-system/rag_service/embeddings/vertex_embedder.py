"""
Vertex AI Embeddings backend.

Uses the Google Cloud text-embedding-005 model (768 dimensions).
Activated when EMBEDDING_BACKEND=vertex_ai.

Prerequisites on GCP:
  - Vertex AI API enabled in the project
  - The Cloud Run Service Account must have the role:
      roles/aiplatform.user

Local usage (requires gcloud auth application-default login):
  EMBEDDING_BACKEND=vertex_ai
  VERTEX_AI_PROJECT=my-project
  VERTEX_AI_LOCATION=us-central1   # optional, default us-central1
"""

from __future__ import annotations

import os
from functools import lru_cache

_PROJECT    = os.getenv("VERTEX_AI_PROJECT")
_LOCATION   = os.getenv("VERTEX_AI_LOCATION", "us-central1")
_MODEL_NAME = os.getenv("VERTEX_AI_EMBEDDING_MODEL", "text-embedding-005")
_DIMENSION  = 768   # text-embedding-005 output dimension


@lru_cache(maxsize=1)
def _get_model():
    """Lazily import and initialise Vertex AI to avoid import-time side effects."""
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel
    except ImportError as exc:
        raise ImportError(
            "google-cloud-aiplatform is required for Vertex AI embeddings. "
            "Install it with: pip install google-cloud-aiplatform>=1.70.0"
        ) from exc

    if not _PROJECT:
        raise EnvironmentError(
            "VERTEX_AI_PROJECT must be set when using EMBEDDING_BACKEND=vertex_ai"
        )

    vertexai.init(project=_PROJECT, location=_LOCATION)
    model = TextEmbeddingModel.from_pretrained(_MODEL_NAME)
    print(f"[VertexEmbedder] Using model '{_MODEL_NAME}' "
          f"(project={_PROJECT}, location={_LOCATION}, dim={_DIMENSION})")
    return model


def get_embedding_dimension() -> int:
    return _DIMENSION


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Return a list of 768-dim embedding vectors for the given texts."""
    model = _get_model()
    # Vertex AI accepts batches of up to 250 texts
    results: list[list[float]] = []
    batch_size = 250
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        embeddings = model.get_embeddings(batch)
        results.extend(e.values for e in embeddings)
    return results


def embed_query(query: str) -> list[float]:
    """Return a single 768-dim embedding vector for a search query."""
    return embed_texts([query])[0]
