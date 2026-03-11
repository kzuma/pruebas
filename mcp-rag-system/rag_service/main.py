"""
RAG Service – FastAPI application.

Provides HTTP endpoints consumed by the MCP server:
  POST /search                  → semantic search
  POST /ingest/google-drive     → ingest files from Google Drive
  GET  /documents               → list stored documents
  DELETE /documents             → delete a document by source
  GET  /collections/{name}/stats → Qdrant collection stats
  GET  /health                  → health check
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from embeddings import get_embedding_dimension
from ingestion import chunk_text, download_drive_file, extract_text, list_drive_files
from retrieval import (
    collection_stats,
    delete_by_source,
    ensure_collection,
    list_documents,
    search,
    upsert_chunks,
)

_DEFAULT_COLLECTION = os.getenv("COLLECTION_NAME", "documents")


# ---------------------------------------------------------------------------
# Lifespan: warm up model & Qdrant connection on startup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[RAG] Warming up embedding model ...")
    dim = get_embedding_dimension()
    print(f"[RAG] Embedding dimension: {dim}")
    await ensure_collection(_DEFAULT_COLLECTION)
    print(f"[RAG] Ready. Default collection: '{_DEFAULT_COLLECTION}'")
    yield
    print("[RAG] Shutting down")


app = FastAPI(
    title="RAG Service",
    description="Retrieval-Augmented Generation service backed by Qdrant + sentence-transformers",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    collection: str = Field(default=_DEFAULT_COLLECTION)


class SearchResult(BaseModel):
    text: str
    score: float
    source: str
    title: str
    chunk_index: int
    ingested_at: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    total: int


class IngestDriveRequest(BaseModel):
    folder_id: str | None = None
    file_id: str | None = None
    collection: str = Field(default=_DEFAULT_COLLECTION)


class IngestResponse(BaseModel):
    ingested_files: list[str]
    total_chunks: int
    collection: str
    errors: list[str] = []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "embedding_dim": get_embedding_dimension()})


@app.post("/search", response_model=SearchResponse)
async def search_endpoint(body: SearchRequest) -> SearchResponse:
    results = await search(
        collection=body.collection,
        query=body.query,
        top_k=body.top_k,
    )
    return SearchResponse(
        query=body.query,
        results=[SearchResult(**r) for r in results],
        total=len(results),
    )


@app.post("/ingest/google-drive", response_model=IngestResponse)
async def ingest_google_drive(body: IngestDriveRequest) -> IngestResponse:
    if not body.folder_id and not body.file_id:
        raise HTTPException(400, "Provide folder_id or file_id")

    # Run sync Drive API in thread pool to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    try:
        files = await loop.run_in_executor(
            None,
            lambda: list_drive_files(
                folder_id=body.folder_id,
                file_id=body.file_id,
            ),
        )
    except Exception as exc:
        raise HTTPException(502, f"Google Drive error: {exc}") from exc

    if not files:
        return IngestResponse(
            ingested_files=[],
            total_chunks=0,
            collection=body.collection,
            errors=["No supported files found at the given location"],
        )

    ingested: list[str] = []
    errors: list[str] = []
    total_chunks = 0

    for f in files:
        try:
            raw_bytes, mime = await loop.run_in_executor(
                None,
                lambda fid=f["id"], mt=f["mimeType"]: download_drive_file(fid, mt),
            )
            text = extract_text(raw_bytes, mime, f["name"])
            if not text.strip():
                errors.append(f"{f['name']}: extracted empty text, skipped")
                continue

            chunks = chunk_text(
                text=text,
                source=f["id"],
                title=f["name"],
                extra_metadata={"drive_file_id": f["id"]},
            )
            count = await upsert_chunks(collection=body.collection, chunks=chunks)
            total_chunks += count
            ingested.append(f["name"])
            print(f"[RAG] Ingested '{f['name']}' → {count} chunks")

        except Exception as exc:
            errors.append(f"{f['name']}: {exc}")
            print(f"[RAG] Error ingesting '{f['name']}': {exc}")

    return IngestResponse(
        ingested_files=ingested,
        total_chunks=total_chunks,
        collection=body.collection,
        errors=errors,
    )


@app.get("/documents")
async def list_docs_endpoint(
    collection: str = Query(default=_DEFAULT_COLLECTION),
    limit: int = Query(default=50, ge=1, le=200),
) -> JSONResponse:
    docs = await list_documents(collection=collection, limit=limit)
    return JSONResponse({"documents": docs, "total": len(docs)})


@app.delete("/documents")
async def delete_document_endpoint(
    source: str = Query(...),
    collection: str = Query(default=_DEFAULT_COLLECTION),
) -> JSONResponse:
    op_id = await delete_by_source(collection=collection, source=source)
    return JSONResponse({"deleted": True, "source": source, "operation_id": op_id})


@app.get("/collections/{collection}/stats")
async def stats_endpoint(collection: str) -> JSONResponse:
    stats = await collection_stats(collection=collection)
    return JSONResponse(stats)
