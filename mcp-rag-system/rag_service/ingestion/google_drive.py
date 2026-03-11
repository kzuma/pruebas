"""
Google Drive integration for the RAG ingestion pipeline.

Authentication options (in order of precedence):
  1. Service Account JSON file at GOOGLE_CREDENTIALS_FILE path.
  2. Application Default Credentials (ADC) — works on GCP, Cloud Run, etc.

Supported MIME types for ingestion:
  - application/vnd.google-apps.document  → exported as plain text
  - application/pdf
  - text/plain
  - text/markdown
  - application/vnd.openxmlformats-officedocument.wordprocessingml.document (.docx)

Usage:
    files = await list_drive_files(folder_id="1abc...")
    for f in files:
        content, mime = await download_drive_file(f["id"], f["mimeType"])
        chunks = chunk_text(content, source=f["id"], title=f["name"])
        ...
"""

from __future__ import annotations

import io
import os
from functools import lru_cache
from typing import Any

import googleapiclient.discovery as gd
from google.oauth2 import service_account
from google.auth import default as google_auth_default
import google.auth.transport.requests

_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "/app/credentials/google_credentials.json")

# MIME types we can ingest
SUPPORTED_MIME_TYPES = {
    "application/vnd.google-apps.document",
    "application/pdf",
    "text/plain",
    "text/markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# Google Docs → export MIME
_GDOC_EXPORT_MIME = "text/plain"


@lru_cache(maxsize=1)
def _get_credentials():
    """Build Google credentials. Cached for the lifetime of the process."""
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]

    if os.path.exists(_CREDENTIALS_FILE):
        print(f"[Drive] Using service account: {_CREDENTIALS_FILE}")
        creds = service_account.Credentials.from_service_account_file(
            _CREDENTIALS_FILE, scopes=scopes
        )
        return creds

    print("[Drive] Using Application Default Credentials")
    creds, _ = google_auth_default(scopes=scopes)
    return creds


def _drive_service():
    """Return an authenticated Drive API v3 resource."""
    creds = _get_credentials()
    # Refresh if needed
    if hasattr(creds, "token") and creds.expired:
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
    return gd.build("drive", "v3", credentials=creds, cache_discovery=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_drive_files(
    folder_id: str | None = None,
    file_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    List supported files from Google Drive.

    Args:
        folder_id: List all supported files inside this folder (recursive).
        file_id:   Return metadata for a single specific file.

    Returns:
        List of dicts: {id, name, mimeType, size}
    """
    service = _drive_service()

    if file_id:
        meta = (
            service.files()
            .get(fileId=file_id, fields="id, name, mimeType, size")
            .execute()
        )
        if meta["mimeType"] not in SUPPORTED_MIME_TYPES:
            return []
        return [meta]

    if folder_id:
        return _list_folder_recursive(service, folder_id)

    raise ValueError("Provide either folder_id or file_id")


def _list_folder_recursive(service, folder_id: str) -> list[dict[str, Any]]:
    """Walk a Drive folder recursively and return supported files."""
    results: list[dict] = []
    page_token: str | None = None

    while True:
        kwargs: dict = dict(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, size)",
            pageSize=100,
        )
        if page_token:
            kwargs["pageToken"] = page_token

        response = service.files().list(**kwargs).execute()
        for f in response.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                # Recurse into sub-folders
                results.extend(_list_folder_recursive(service, f["id"]))
            elif f["mimeType"] in SUPPORTED_MIME_TYPES:
                results.append(f)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def download_drive_file(file_id: str, mime_type: str) -> tuple[bytes, str]:
    """
    Download a Drive file as bytes.

    Google Docs are exported as plain text. Binary files are downloaded directly.

    Returns:
        (bytes_content, effective_mime_type)
    """
    service = _drive_service()

    if mime_type == "application/vnd.google-apps.document":
        # Export Google Doc as plain text
        data = (
            service.files()
            .export_media(fileId=file_id, mimeType=_GDOC_EXPORT_MIME)
            .execute()
        )
        return data, _GDOC_EXPORT_MIME

    # Download binary file
    import googleapiclient.http as gh
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = gh.MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue(), mime_type
