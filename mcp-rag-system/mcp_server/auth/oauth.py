"""
OAuth 2.0 Authorization Server implementation for MCP.

Implements the Authorization Code Flow as required by the MCP spec:
  - GET  /.well-known/oauth-authorization-server  → server metadata
  - POST /oauth/register                           → dynamic client registration
  - GET  /oauth/authorize                          → authorization endpoint
  - POST /oauth/token                              → token endpoint
  - POST /oauth/revoke                             → token revocation

State backend selection (controlled by REDIS_URL env var):
  - REDIS_URL set   → RedisStateBackend  (Cloud Run / production)
  - REDIS_URL unset → MemoryStateBackend (local Docker Compose)

In production replace MemoryStateBackend with RedisStateBackend by setting
REDIS_URL to the Cloud Memorystore private IP:
  REDIS_URL=redis://10.0.0.3:6379
"""

from __future__ import annotations

import json
import secrets
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from .settings import settings

router = APIRouter()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# State backends — dual: in-memory (local) or Redis (Cloud Run)
# ---------------------------------------------------------------------------

class _StateBackend(ABC):
    """Abstract key/value + set store used for OAuth state."""

    # Key-value (str → str, with optional TTL in seconds)
    @abstractmethod
    async def kv_get(self, key: str) -> str | None: ...
    @abstractmethod
    async def kv_set(self, key: str, value: str, ttl: int | None = None) -> None: ...
    @abstractmethod
    async def kv_delete(self, key: str) -> None: ...
    @abstractmethod
    async def kv_getdel(self, key: str) -> str | None: ...  # atomic get+delete

    # Set (for revoked JTIs)
    @abstractmethod
    async def set_add(self, key: str, member: str) -> None: ...
    @abstractmethod
    async def set_contains(self, key: str, member: str) -> bool: ...


class _MemoryStateBackend(_StateBackend):
    """In-memory backend. Single-instance only — use for local dev."""

    def __init__(self) -> None:
        self._kv: dict[str, tuple[str, float | None]] = {}  # key → (value, expires_at)
        self._sets: dict[str, set[str]] = {}

    def _is_expired(self, key: str) -> bool:
        if key not in self._kv:
            return True
        _, exp = self._kv[key]
        return exp is not None and time.time() > exp

    async def kv_get(self, key: str) -> str | None:
        if self._is_expired(key):
            self._kv.pop(key, None)
            return None
        return self._kv[key][0]

    async def kv_set(self, key: str, value: str, ttl: int | None = None) -> None:
        exp = time.time() + ttl if ttl else None
        self._kv[key] = (value, exp)

    async def kv_delete(self, key: str) -> None:
        self._kv.pop(key, None)

    async def kv_getdel(self, key: str) -> str | None:
        value = await self.kv_get(key)
        await self.kv_delete(key)
        return value

    async def set_add(self, key: str, member: str) -> None:
        self._sets.setdefault(key, set()).add(member)

    async def set_contains(self, key: str, member: str) -> bool:
        return member in self._sets.get(key, set())


class _RedisStateBackend(_StateBackend):
    """Redis-backed backend for Cloud Run (Cloud Memorystore)."""

    def __init__(self, redis_url: str) -> None:
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(redis_url, decode_responses=True)

    async def kv_get(self, key: str) -> str | None:
        return await self._redis.get(key)

    async def kv_set(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl:
            await self._redis.setex(key, ttl, value)
        else:
            await self._redis.set(key, value)

    async def kv_delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def kv_getdel(self, key: str) -> str | None:
        # Redis GETDEL is atomic (available since Redis 6.2)
        return await self._redis.getdel(key)

    async def set_add(self, key: str, member: str) -> None:
        await self._redis.sadd(key, member)

    async def set_contains(self, key: str, member: str) -> bool:
        return bool(await self._redis.sismember(key, member))


def _build_backend() -> _StateBackend:
    if settings.redis_url:
        print(f"[OAuth] Using Redis backend: {settings.redis_url[:20]}***")
        return _RedisStateBackend(settings.redis_url)
    print("[OAuth] Using in-memory backend (set REDIS_URL for production)")
    return _MemoryStateBackend()


# Module-level backend instance
_state = _build_backend()

# Namespace prefixes for Redis keys
_NS_USER    = "oauth:user:"
_NS_CLIENT  = "oauth:client:"
_NS_CODE    = "oauth:code:"
_NS_REVOKED = "oauth:revoked_jtis"


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


async def verify_access_token(token: str) -> dict:
    """Decode and validate a bearer token. Raises HTTPException on failure."""
    try:
        payload = jwt.decode(token, settings.oauth_secret_key, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if await _state.set_contains(_NS_REVOKED, payload.get("jti", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


async def _bootstrap_admin() -> None:
    """Create the admin user if it does not exist (called on startup)."""
    key = f"{_NS_USER}{settings.admin_username}"
    if not await _state.kv_get(key):
        hashed = pwd_context.hash(settings.admin_password)
        await _state.kv_set(key, hashed)


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
    client_data = {
        "client_name": body.client_name,
        "redirect_uris": body.redirect_uris,
        "grant_types": body.grant_types,
        "response_types": body.response_types,
        "scope": body.scope,
        "client_secret": client_secret,
    }
    await _state.kv_set(f"{_NS_CLIENT}{client_id}", json.dumps(client_data))
    return JSONResponse({"client_id": client_id, **client_data})


async def _get_client(client_id: str) -> dict:
    raw = await _state.kv_get(f"{_NS_CLIENT}{client_id}")
    if not raw:
        raise HTTPException(400, f"Unknown client_id: {client_id}")
    return json.loads(raw)


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
    client = await _get_client(client_id)
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
    client = await _get_client(client_id)
    if redirect_uri not in client["redirect_uris"]:
        raise HTTPException(400, "Invalid redirect_uri")

    # Authenticate user
    hashed = await _state.kv_get(f"{_NS_USER}{username}")
    if not hashed or not _verify_password(password, hashed):
        raise HTTPException(401, "Invalid credentials")

    # Issue authorization code (single-use, 5-minute TTL)
    code = secrets.token_urlsafe(32)
    code_data = json.dumps({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "username": username,
    })
    await _state.kv_set(f"{_NS_CODE}{code}", code_data, ttl=300)

    params = {"code": code, "state": state}
    return RedirectResponse(url=f"{redirect_uri}?{urlencode(params)}", status_code=302)


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

    # Atomic get-and-delete to prevent code reuse
    raw = await _state.kv_getdel(f"{_NS_CODE}{code}")
    if not raw:
        raise HTTPException(400, "Invalid or expired authorization code")

    auth_data = json.loads(raw)

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
        await _state.set_add(_NS_REVOKED, payload["jti"])
    except JWTError:
        pass  # Per RFC 7009, always return 200
    return JSONResponse({"revoked": True})
