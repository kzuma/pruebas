"""
Document processing: text extraction and chunking.

Supports:
  - Plain text (.txt, .md)
  - PDF (via pypdf)
  - DOCX (via python-docx)
  - Google Docs exported as plain text
"""

from __future__ import annotations

import io
import os
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

_CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "512"))
_CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "64"))

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=_CHUNK_SIZE,
    chunk_overlap=_CHUNK_OVERLAP,
    separators=["\n\n", "\n", ". ", " ", ""],
)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n\n".join(p for p in pages if p.strip())


def _extract_docx(data: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(data))
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


def extract_text(data: bytes, mime_type: str, filename: str) -> str:
    """
    Extract plain text from document bytes.

    Args:
        data:      Raw file bytes.
        mime_type: MIME type string.
        filename:  Original file name (used as fallback heuristic).

    Returns:
        Extracted text as a single string.
    """
    mime_type = mime_type.lower()

    if "pdf" in mime_type or filename.lower().endswith(".pdf"):
        return _extract_pdf(data)

    if (
        "wordprocessingml" in mime_type
        or "vnd.openxmlformats-officedocument.wordprocessingml" in mime_type
        or filename.lower().endswith(".docx")
    ):
        return _extract_docx(data)

    # Plain text, Markdown, Google Docs exported text, etc.
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(
    text: str,
    source: str,
    title: str,
    extra_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Split text into overlapping chunks ready for embedding.

    Args:
        text:           Full extracted text.
        source:         Unique source identifier (file path or Drive ID).
        title:          Human-readable document title.
        extra_metadata: Additional key-value pairs added to each chunk.

    Returns:
        List of chunk dicts: {text, source, title, chunk_index, ...metadata}
    """
    raw_chunks = _splitter.split_text(text)
    meta = extra_metadata or {}
    return [
        {
            "text": chunk,
            "source": source,
            "title": title,
            "chunk_index": i,
            **meta,
        }
        for i, chunk in enumerate(raw_chunks)
        if chunk.strip()
    ]
