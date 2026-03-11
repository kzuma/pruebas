"""
OAuth 2.0 Authorization Server implementation for MCP.

Implements the Authorization Code Flow as required by the MCP spec:
  - GET  /.well-known/oauth-authorization-server  → server metadata
  - POST /oauth/register                           → dynamic client registration
  - GET  /oauth/authorize                          → authorization endpoint
  - POST /oauth/token                              → token endpoint
  - POST /oauth/revoke                             → token revocation

In production, replace the in-memory stores with a proper database
and use a hardened identity provider (Keycloak, Auth0, etc.).
"""

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from .settings import settings

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# In-memory stores  (replace with Redis / DB in production)
# ---------------------------------------------------------------------------
_users: dict[str, str] = {}          # username → hashed_password
_clients: dict[str, dict] = {}       # client_id → client metadata
_auth_codes: dict[str, dict] = {}    # code → {client_id, redirect_uri, scope, user}
_revoked_tokens: set[str] = set()    # jti of revoked tokens


def _bootstrap_admin() -> None:
    """Create the admin user from env vars on startup."""
    _users[settings.admin_username] = pwd_context.hash(settings.admin_password)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_access_token(subject: str, client_id: str, scope: str) -> str:
    jti = secrets.token_urlsafe(16)
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.oauth_token_expire_minutes
    )
    payload = {
        "sub": subject,
        "client_id": client_id,
        "scope": scope,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "jti": jti,
    }
    return jwt.encode(payload, settings.oauth_secret_key, algorithm="HS256")


def verify_access_token(token: str) -> dict:
    """Decode and validate a bearer token. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, settings.oauth_secret_key, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("jti") in _revoked_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


# ---------------------------------------------------------------------------
# OAuth 2.0 Metadata  (RFC 8414)
# ---------------------------------------------------------------------------

@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request) -> JSONResponse:
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp:read", "mcp:write"],
        "code_challenge_methods_supported": ["S256"],
    })


# ---------------------------------------------------------------------------
# Dynamic Client Registration  (RFC 7591)
# ---------------------------------------------------------------------------

class ClientRegistrationRequest(BaseModel):
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str] = ["authorization_code"]
    response_types: list[str] = ["code"]
    scope: str = "mcp:read mcp:write"


@router.post("/oauth/register", status_code=201)
async def register_client(body: ClientRegistrationRequest) -> JSONResponse:
    client_id = secrets.token_urlsafe(16)
    client_secret = secrets.token_urlsafe(32)
    _clients[client_id] = {
        "client_name": body.client_name,
        "redirect_uris": body.redirect_uris,
        "grant_types": body.grant_types,
        "response_types": body.response_types,
        "scope": body.scope,
        "client_secret": client_secret,
    }
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.client_name,
        "redirect_uris": body.redirect_uris,
        "grant_types": body.grant_types,
        "response_types": body.response_types,
        "scope": body.scope,
    })


# ---------------------------------------------------------------------------
# Authorization Endpoint
# ---------------------------------------------------------------------------

_AUTHORIZE_FORM = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>Autorizar acceso – MCP RAG Server</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 420px;
            margin: 80px auto; padding: 0 1rem; }}
    h2   {{ color: #1a202c; }}
    p    {{ color: #4a5568; font-size: .9rem; }}
    input[type=text], input[type=password] {{
      width: 100%; padding: .5rem; margin: .25rem 0 1rem;
      border: 1px solid #cbd5e0; border-radius: 4px; font-size: 1rem; }}
    button {{ width: 100%; padding: .6rem; background: #3182ce;
              color: #fff; border: none; border-radius: 4px;
              font-size: 1rem; cursor: pointer; }}
    button:hover {{ background: #2b6cb0; }}
    .app {{ background: #ebf8ff; border-left: 4px solid #3182ce;
            padding: .75rem 1rem; border-radius: 4px; margin-bottom: 1.5rem; }}
  </style>
</head>
<body>
  <h2>Autorizar acceso</h2>
  <div class="app">
    <strong>{client_name}</strong> solicita acceso a:<br>
    <small>Ámbitos: <code>{scope}</code></small>
  </div>
  <form method="post" action="/oauth/authorize">
    <input type="hidden" name="client_id"     value="{client_id}">
    <input type="hidden" name="redirect_uri"  value="{redirect_uri}">
    <input type="hidden" name="state"         value="{state}">
    <input type="hidden" name="scope"         value="{scope}">
    <label>Usuario</label>
    <input type="text" name="username" autofocus required>
    <label>Contraseña</label>
    <input type="password" name="password" required>
    <button type="submit">Autorizar</button>
  </form>
</body>
</html>
"""


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def authorize_get(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query("mcp:read mcp:write"),
    state: str = Query(""),
):
    if response_type != "code":
        raise HTTPException(400, "Only response_type=code is supported")
    if client_id not in _clients:
        raise HTTPException(400, f"Unknown client_id: {client_id}")
    client = _clients[client_id]
    if redirect_uri not in client["redirect_uris"]:
        raise HTTPException(400, "redirect_uri not registered for this client")

    return HTMLResponse(_AUTHORIZE_FORM.format(
        client_id=client_id,
        client_name=client["client_name"],
        redirect_uri=redirect_uri,
        state=state,
        scope=scope,
    ))


@router.post("/oauth/authorize")
async def authorize_post(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    scope: str = Form("mcp:read mcp:write"),
    username: str = Form(...),
    password: str = Form(...),
):
    # Validate client
    if client_id not in _clients:
        raise HTTPException(400, "Unknown client")
    client = _clients[client_id]
    if redirect_uri not in client["redirect_uris"]:
        raise HTTPException(400, "Invalid redirect_uri")

    # Authenticate user
    hashed = _users.get(username)
    if not hashed or not _verify_password(password, hashed):
        raise HTTPException(401, "Invalid credentials")

    # Issue authorization code (single-use, 5-minute TTL)
    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "username": username,
        "expires_at": time.time() + 300,
    }

    params = {"code": code, "state": state}
    return RedirectResponse(
        url=f"{redirect_uri}?{urlencode(params)}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Token Endpoint
# ---------------------------------------------------------------------------

@router.post("/oauth/token")
async def token(
    grant_type: str = Form(...),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    client_id: str = Form(None),
):
    if grant_type != "authorization_code":
        raise HTTPException(400, "Unsupported grant_type")
    if code not in _auth_codes:
        raise HTTPException(400, "Invalid or expired authorization code")

    auth_data = _auth_codes.pop(code)  # single-use

    if time.time() > auth_data["expires_at"]:
        raise HTTPException(400, "Authorization code expired")
    if auth_data["client_id"] != client_id:
        raise HTTPException(400, "client_id mismatch")
    if redirect_uri and auth_data["redirect_uri"] != redirect_uri:
        raise HTTPException(400, "redirect_uri mismatch")

    access_token = _create_access_token(
        subject=auth_data["username"],
        client_id=client_id,
        scope=auth_data["scope"],
    )
    return JSONResponse({
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.oauth_token_expire_minutes * 60,
        "scope": auth_data["scope"],
    })


# ---------------------------------------------------------------------------
# Token Revocation  (RFC 7009)
# ---------------------------------------------------------------------------

@router.post("/oauth/revoke")
async def revoke(token: str = Form(...)):
    try:
        payload = jwt.decode(
            token, settings.oauth_secret_key, algorithms=["HS256"]
        )
        _revoked_tokens.add(payload["jti"])
    except JWTError:
        pass  # Per RFC 7009, always return 200
    return JSONResponse({"revoked": True})


# Bootstrap admin user on import
_bootstrap_admin()
