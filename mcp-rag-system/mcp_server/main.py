"""
MCP Server with OAuth 2.0 – entry point.

Exposes two FastAPI applications mounted on the same Uvicorn process:
  1. The OAuth 2.0 authorization server  (mounted at /)
  2. The MCP SSE / Streamable-HTTP endpoint (mounted at /mcp)

Claude connects to this server via the MCP protocol over HTTP,
authenticating with OAuth 2.0 bearer tokens.

Architecture:
  Claude → POST /mcp/messages  (Bearer token required)
         → GET  /mcp/sse       (Bearer token required)
  Browser → GET /oauth/authorize → POST /oauth/authorize → GET /oauth/token
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

from auth import oauth_router, settings, verify_access_token
from tools import register_tools


# ---------------------------------------------------------------------------
# OAuth dependency for MCP endpoints
# ---------------------------------------------------------------------------

async def require_token(request: Request) -> dict:
    """FastAPI dependency: validates Bearer token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth_header.removeprefix("Bearer ").strip()
    return verify_access_token(token)


# ---------------------------------------------------------------------------
# FastMCP instance  (the actual MCP server)
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name=settings.mcp_server_name,
    instructions=(
        "You are connected to a RAG (Retrieval-Augmented Generation) knowledge base. "
        "Use the available tools to search documents, ingest new content from Google Drive, "
        "and manage the knowledge base to answer user queries with up-to-date context."
    ),
)

# Register all RAG tools
register_tools(mcp)


# ---------------------------------------------------------------------------
# Main FastAPI app (OAuth + health + MCP delegation)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[MCP Server] Starting '{settings.mcp_server_name}'")
    print(f"[MCP Server] RAG service: {settings.rag_service_url}")
    yield
    print("[MCP Server] Shutting down")


app = FastAPI(
    title="MCP RAG Server",
    description="MCP server with OAuth 2.0 and RAG capabilities",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS – restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount OAuth routes
app.include_router(oauth_router)


# ---------------------------------------------------------------------------
# Health & info endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "server": settings.mcp_server_name})


@app.get("/")
async def root(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "server": settings.mcp_server_name,
        "mcp_endpoint": f"{base}/mcp",
        "oauth_metadata": f"{base}/.well-known/oauth-authorization-server",
        "docs": f"{base}/docs",
    })


# ---------------------------------------------------------------------------
# MCP endpoints with OAuth protection
# ---------------------------------------------------------------------------
# FastMCP's streamable-HTTP transport handles POST /mcp/messages.
# We wrap it with a sub-application that enforces our OAuth middleware.

class _OAuthMiddleware:
    """ASGI middleware that validates bearer tokens before forwarding to MCP."""

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if not auth.startswith("Bearer "):
                response = JSONResponse(
                    {"detail": "Missing Bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            token = auth.removeprefix("Bearer ").strip()
            try:
                verify_access_token(token)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


# Get the MCP ASGI app and protect it
_mcp_app = mcp.streamable_http_app()
_protected_mcp = _OAuthMiddleware(_mcp_app)

# Mount at /mcp
app.mount("/mcp", _protected_mcp)
