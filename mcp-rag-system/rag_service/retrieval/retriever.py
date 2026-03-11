"""
Qdrant vector store client.

Handles:
  - Collection creation / management
  - Upsert of embedded chunks
  - Semantic search (cosine similarity)
  - Document deletion by source
  - Collection stats
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from embeddings import embed_query, embed_texts, get_embedding_dimension

_QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
_QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
_QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")  # optional


def _client() -> AsyncQdrantClient:
    """Return a new async Qdrant client (lightweight, stateless)."""
    return AsyncQdrantClient(
        host=_QDRANT_HOST,
        port=_QDRANT_PORT,
        api_key=_QDRANT_API_KEY,
    )


async def ensure_collection(collection: str) -> None:
    """Create the collection if it does not exist yet."""
    dim = get_embedding_dimension()
    async with _client() as qc:
        existing = [c.name for c in (await qc.get_collections()).collections]
        if collection not in existing:
            await qc.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=dim,
                    distance=models.Distance.COSINE,
                ),
            )
            # Payload index for efficient filtering by source
            await qc.create_payload_index(
                collection_name=collection,
                field_name="source",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
            print(f"[Qdrant] Created collection '{collection}' (dim={dim})")


async def upsert_chunks(
    collection: str,
    chunks: list[dict[str, Any]],
) -> int:
    """
    Embed and store a list of text chunks.

    Each chunk dict must have:
      - text (str)       – the raw text
      - source (str)     – origin file name or Drive file ID
      - title (str)      – document title
      - chunk_index (int)– position within the document
    Optional extra keys are stored as metadata.

    Returns the number of points upserted.
    """
    if not chunks:
        return 0

    await ensure_collection(collection)

    texts = [c["text"] for c in chunks]
    vectors = embed_texts(texts)
    now = datetime.now(timezone.utc).isoformat()

    points = [
        models.PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload={
                **{k: v for k, v in chunk.items() if k != "text"},
                "text": chunk["text"],
                "ingested_at": now,
            },
        )
        for chunk, vector in zip(chunks, vectors)
    ]

    async with _client() as qc:
        await qc.upsert(collection_name=collection, points=points)

    return len(points)


async def search(
    collection: str,
    query: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Perform a semantic search and return the top-k results.

    Returns a list of dicts: {text, score, source, title, chunk_index, ...}
    """
    await ensure_collection(collection)
    query_vector = embed_query(query)

    async with _client() as qc:
        results = await qc.search(
            collection_name=collection,
            query_vector=query_vector,
            limit=top_k,
            with_payload=True,
        )

    return [
        {
            "text": r.payload.get("text", ""),
            "score": round(r.score, 4),
            "source": r.payload.get("source", ""),
            "title": r.payload.get("title", ""),
            "chunk_index": r.payload.get("chunk_index", 0),
            "ingested_at": r.payload.get("ingested_at", ""),
        }
        for r in results
    ]


async def delete_by_source(collection: str, source: str) -> int:
    """Delete all points whose `source` payload matches the given value."""
    await ensure_collection(collection)
    async with _client() as qc:
        result = await qc.delete(
            collection_name=collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source",
                            match=models.MatchValue(value=source),
                        )
                    ]
                )
            ),
        )
    return result.operation_id  # Qdrant returns operation_id; actual count requires scroll


async def list_documents(collection: str, limit: int = 50) -> list[dict[str, Any]]:
    """
    Return unique documents (by source) stored in the collection.
    Uses scroll to avoid large in-memory loads.
    """
    await ensure_collection(collection)
    seen: dict[str, dict] = {}
    offset = None

    async with _client() as qc:
        while len(seen) < limit:
            records, offset = await qc.scroll(
                collection_name=collection,
                limit=100,
                offset=offset,
                with_payload=["source", "title", "ingested_at"],
                with_vectors=False,
            )
            for r in records:
                src = r.payload.get("source", "")
                if src and src not in seen:
                    seen[src] = {
                        "source": src,
                        "title": r.payload.get("title", src),
                        "ingested_at": r.payload.get("ingested_at", ""),
                    }
            if offset is None or len(seen) >= limit:
                break

    return list(seen.values())[:limit]


async def collection_stats(collection: str) -> dict[str, Any]:
    """Return info about a Qdrant collection."""
    await ensure_collection(collection)
    async with _client() as qc:
        info = await qc.get_collection(collection_name=collection)
    return {
        "vectors_count": info.vectors_count,
        "points_count": info.points_count,
        "status": info.status,
        "config": {
            "dimension": info.config.params.vectors.size,
            "distance": info.config.params.vectors.distance.value,
        },
    }
