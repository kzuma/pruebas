"""
MCP tools that proxy requests to the RAG service.

Each function decorated with @mcp.tool() becomes a callable tool
that Claude can invoke through the MCP protocol.
"""

from __future__ import annotations

import httpx
from mcp.server.fastmcp import FastMCP

from auth.settings import settings

# This module receives the shared FastMCP instance from main.py
# Tools are registered via the `register_tools` helper below.


def register_tools(mcp: FastMCP) -> None:
    """Register all RAG tools on the given FastMCP instance."""

    rag_url = settings.rag_service_url

    # ------------------------------------------------------------------
    # Tool: search_knowledge_base
    # ------------------------------------------------------------------

    @mcp.tool()
    async def search_knowledge_base(
        query: str,
        top_k: int = 5,
        collection: str = "documents",
    ) -> dict:
        """
        Search the RAG knowledge base using semantic similarity.

        Args:
            query:      The natural-language question or search phrase.
            top_k:      Number of results to return (1-20, default 5).
            collection: Qdrant collection name (default "documents").

        Returns:
            A dict with keys:
            - results: list of {text, score, metadata} dicts
            - query:   the original query
            - total:   number of results returned
        """
        top_k = max(1, min(top_k, 20))
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{rag_url}/search",
                json={"query": query, "top_k": top_k, "collection": collection},
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Tool: ingest_google_drive
    # ------------------------------------------------------------------

    @mcp.tool()
    async def ingest_google_drive(
        folder_id: str | None = None,
        file_id: str | None = None,
        collection: str = "documents",
    ) -> dict:
        """
        Ingest files from Google Drive into the RAG knowledge base.

        Provide either a folder_id (to ingest all supported files in
        that folder) or a specific file_id.

        Supported file types: Google Docs, PDF, plain text, Markdown.

        Args:
            folder_id:  Google Drive folder ID to ingest recursively.
            file_id:    Single Google Drive file ID to ingest.
            collection: Target Qdrant collection (default "documents").

        Returns:
            A dict with:
            - ingested_files: list of file names processed
            - total_chunks:   number of vector chunks stored
            - collection:     target collection name
        """
        if not folder_id and not file_id:
            return {"error": "Provide either folder_id or file_id"}

        payload: dict = {"collection": collection}
        if folder_id:
            payload["folder_id"] = folder_id
        if file_id:
            payload["file_id"] = file_id

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(f"{rag_url}/ingest/google-drive", json=payload)
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Tool: list_documents
    # ------------------------------------------------------------------

    @mcp.tool()
    async def list_documents(
        collection: str = "documents",
        limit: int = 50,
    ) -> dict:
        """
        List documents currently stored in the knowledge base.

        Args:
            collection: Qdrant collection to inspect (default "documents").
            limit:      Max number of document entries to return (1-200).

        Returns:
            A dict with:
            - documents: list of {id, source, title, ingested_at} dicts
            - total:     total count in the collection
        """
        limit = max(1, min(limit, 200))
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{rag_url}/documents",
                params={"collection": collection, "limit": limit},
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Tool: delete_document
    # ------------------------------------------------------------------

    @mcp.tool()
    async def delete_document(
        source: str,
        collection: str = "documents",
    ) -> dict:
        """
        Remove all chunks of a document from the knowledge base by its source path.

        Args:
            source:     The document source identifier (file name or Drive ID).
            collection: Qdrant collection (default "documents").

        Returns:
            A dict with `deleted_chunks` count and `source`.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.delete(
                f"{rag_url}/documents",
                params={"source": source, "collection": collection},
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Tool: get_collection_stats
    # ------------------------------------------------------------------

    @mcp.tool()
    async def get_collection_stats(collection: str = "documents") -> dict:
        """
        Return statistics about a Qdrant vector collection.

        Args:
            collection: Collection name (default "documents").

        Returns:
            A dict with `vectors_count`, `points_count`, `status`, and
            `config` (vector dimension, distance metric).
        """
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{rag_url}/collections/{collection}/stats"
            )
            response.raise_for_status()
            return response.json()
