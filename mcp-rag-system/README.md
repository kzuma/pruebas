# MCP RAG System

Sistema de agentes con **Model Context Protocol (MCP)**, autenticación **OAuth 2.0** y **RAG** (Retrieval-Augmented Generation) respaldado por Google Drive y Qdrant.

Claude se conecta al servidor MCP como cliente autenticado, y dispone de herramientas para buscar documentos, ingestar contenido desde Google Drive y gestionar la base de conocimiento vectorial.

---

## Arquitectura

```
Claude Desktop / claude.ai
         │
         │  MCP + OAuth 2.0 Bearer Token
         ▼
┌─────────────────────────┐
│    MCP Server :8000     │  FastMCP · FastAPI · OAuth 2.0 AS
│  /oauth/authorize       │
│  /mcp  (protegido)      │
└───────────┬─────────────┘
            │  HTTP (red interna Docker)
            ▼
┌─────────────────────────┐
│   RAG Service :8001     │  FastAPI · sentence-transformers
│  /search                │
│  /ingest/google-drive   │
└──────┬──────────────────┘
       │
  ┌────▼─────┐   ┌──────────────────┐
  │  Qdrant  │   │  Google Drive    │
  │  :6333   │   │  API v3          │
  └──────────┘   └──────────────────┘
```

### Herramientas MCP disponibles

| Herramienta | Descripción |
|---|---|
| `search_knowledge_base` | Búsqueda semántica (cosine similarity) |
| `ingest_google_drive` | Ingesta carpeta o fichero de Google Drive |
| `list_documents` | Lista documentos almacenados en la colección |
| `delete_document` | Elimina todos los chunks de un documento |
| `get_collection_stats` | Estadísticas de la colección Qdrant |

---

## Prerrequisitos

| Herramienta | Versión mínima | Verificar |
|---|---|---|
| Docker | 24.x | `docker --version` |
| Docker Compose | 2.x (plugin) | `docker compose version` |
| Python | 3.12 (solo para dev local sin Docker) | `python --version` |
| Cuenta de Google Cloud | — | console.cloud.google.com |

---

## Instalación paso a paso

### Paso 1 — Clonar el repositorio

```bash
git clone <url-del-repositorio>
cd mcp-rag-system
```

### Paso 2 — Configurar variables de entorno

```bash
cp .env.example .env
```

Edita `.env` con tu editor y ajusta los valores:

```bash
# Clave secreta para firmar los JWT de OAuth (mínimo 32 caracteres)
# Genera una clave segura con:  python -c "import secrets; print(secrets.token_urlsafe(32))"
OAUTH_SECRET_KEY=reemplaza-con-una-clave-aleatoria-de-al-menos-32-caracteres

# Usuario administrador para el formulario de login OAuth
ADMIN_USERNAME=admin
ADMIN_PASSWORD=una-contraseña-segura

# Tiempo de vida de los tokens en minutos
OAUTH_TOKEN_EXPIRE_MINUTES=60
```

Los demás valores pueden quedarse como están para desarrollo local.

### Paso 3 — Configurar credenciales de Google Drive

Tienes dos opciones. Elige la que mejor se adapte a tu caso.

#### Opción A: Service Account (recomendada para servidores)

1. Ve a [Google Cloud Console](https://console.cloud.google.com) y crea un proyecto (o usa uno existente).

2. Activa la **Google Drive API**:
   - Menú → APIs y servicios → Biblioteca → busca "Google Drive API" → Activar

3. Crea una Service Account:
   - Menú → APIs y servicios → Credenciales → Crear credenciales → Cuenta de servicio
   - Nombre: `rag-reader` (o el que prefieras)
   - Rol: ninguno necesario a nivel de proyecto

4. Genera una clave JSON:
   - Entra en la Service Account → Claves → Agregar clave → JSON
   - Descarga el fichero

5. Coloca el fichero en la carpeta del proyecto:
   ```bash
   mkdir -p credentials
   cp ~/Descargas/tu-service-account-xxxx.json credentials/google_credentials.json
   ```

6. Comparte las carpetas de Google Drive con el email de la Service Account:
   - Abre la carpeta en Drive → Compartir → pega el email de la SA (termina en `@*.iam.gserviceaccount.com`) → rol Lector

#### Opción B: Application Default Credentials (para desarrollo local)

Si ya tienes `gcloud` CLI instalado y autenticado:

```bash
gcloud auth application-default login
```

El sistema detectará automáticamente las credenciales. No necesitas el fichero JSON.

> Si no tienes Google Drive configurado, puedes arrancar el sistema igualmente.
> La ingesta fallará, pero la búsqueda sobre datos ya cargados funcionará.

### Paso 4 — Construir y arrancar los servicios

```bash
docker compose up --build
```

La primera vez tardará varios minutos porque:
- Construye las imágenes Docker
- Descarga y guarda el modelo de embeddings `all-MiniLM-L6-v2` (~90 MB) dentro de la imagen

Cuando veas estos mensajes, el sistema está listo:

```
rag_service   | [RAG] Embedding dimension: 384
rag_service   | [RAG] Ready. Default collection: 'documents'
mcp_server    | [MCP Server] Starting 'rag-mcp-server'
mcp_server    | INFO:     Application startup complete.
```

Para arranques posteriores (sin reconstruir):

```bash
docker compose up
```

Para parar:

```bash
docker compose down
```

Para parar y eliminar los datos de Qdrant:

```bash
docker compose down -v
```

---

## Verificar que todo funciona

### Health checks

```bash
# RAG Service
curl http://localhost:8001/health
# → {"status":"ok","embedding_dim":384}

# MCP Server
curl http://localhost:8000/health
# → {"status":"ok","server":"rag-mcp-server"}

# Qdrant
curl http://localhost:6333/health
# → {"title":"qdrant - vector search engine","version":"..."}
```

### Obtener un token OAuth (para pruebas)

#### 1. Registrar un cliente OAuth

```bash
curl -s -X POST http://localhost:8000/oauth/register \
  -H "Content-Type: application/json" \
  -d '{
    "client_name": "mi-cliente-test",
    "redirect_uris": ["http://localhost:9999/callback"]
  }' | python -m json.tool
```

Guarda el `client_id` y `client_secret` del resultado.

#### 2. Abrir el flujo de autorización en el navegador

Abre en tu navegador (sustituye `CLIENT_ID` con el valor del paso anterior):

```
http://localhost:8000/oauth/authorize?response_type=code&client_id=CLIENT_ID&redirect_uri=http://localhost:9999/callback
```

Introduce las credenciales del administrador (las de `.env`) y haz clic en Autorizar.

El navegador redirigirá a `http://localhost:9999/callback?code=AUTHORIZATION_CODE&state=`.
Copia el valor del parámetro `code`.

#### 3. Intercambiar el código por un token

```bash
curl -s -X POST http://localhost:8000/oauth/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=AUTHORIZATION_CODE&client_id=CLIENT_ID&redirect_uri=http://localhost:9999/callback" \
  | python -m json.tool
```

Resultado:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiJ9...",
  "token_type": "bearer",
  "expires_in": 3600,
  "scope": "mcp:read mcp:write"
}
```

Exporta el token para los siguientes pasos:
```bash
export TOKEN="eyJhbGciOiJIUzI1NiJ9..."
```

### Ingestar un fichero de Google Drive

```bash
# Por ID de fichero
curl -s -X POST http://localhost:8001/ingest/google-drive \
  -H "Content-Type: application/json" \
  -d '{"file_id": "TU_DRIVE_FILE_ID", "collection": "documents"}' \
  | python -m json.tool

# Por ID de carpeta (recursivo)
curl -s -X POST http://localhost:8001/ingest/google-drive \
  -H "Content-Type: application/json" \
  -d '{"folder_id": "TU_DRIVE_FOLDER_ID", "collection": "documents"}' \
  | python -m json.tool
```

> El `file_id` o `folder_id` se obtiene de la URL de Google Drive:
> `https://drive.google.com/drive/folders/`**`1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs`**

Resultado esperado:
```json
{
  "ingested_files": ["Documento de ejemplo.docx", "informe.pdf"],
  "total_chunks": 47,
  "collection": "documents",
  "errors": []
}
```

### Hacer una búsqueda semántica

```bash
curl -s -X POST http://localhost:8001/search \
  -H "Content-Type: application/json" \
  -d '{"query": "¿Cuáles son los objetivos del proyecto?", "top_k": 3}' \
  | python -m json.tool
```

Resultado:
```json
{
  "query": "¿Cuáles son los objetivos del proyecto?",
  "results": [
    {
      "text": "Los objetivos principales del proyecto son...",
      "score": 0.8934,
      "source": "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs",
      "title": "Plan de proyecto 2026.docx",
      "chunk_index": 2,
      "ingested_at": "2026-03-11T10:00:00+00:00"
    }
  ],
  "total": 3
}
```

### Listar documentos

```bash
curl -s "http://localhost:8001/documents?limit=10" | python -m json.tool
```

### Estadísticas de la colección

```bash
curl -s "http://localhost:8001/collections/documents/stats" | python -m json.tool
```

---

## Conectar Claude Desktop al servidor MCP

### Opción A: Sin autenticación (solo desarrollo local)

Si quieres probar las herramientas MCP sin pasar por el flujo OAuth, puedes hacer una prueba rápida ejecutando el MCP server directamente con `fastmcp`:

1. Instala las dependencias localmente:
   ```bash
   cd mcp_server
   pip install -r requirements.txt
   ```

2. Edita `~/.claude/claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "rag": {
         "command": "python",
         "args": ["/ruta/absoluta/a/mcp-rag-system/mcp_server/main.py"],
         "env": {
           "RAG_SERVICE_URL": "http://localhost:8001",
           "OAUTH_SECRET_KEY": "dev-secret-key-32chars-minimum"
         }
       }
     }
   }
   ```

3. Reinicia Claude Desktop. Verás el icono de herramientas (🔧) en la interfaz.

### Opción B: Con OAuth 2.0 (recomendada)

Con los servicios levantados vía Docker Compose, añade al `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rag-knowledge-base": {
      "url": "http://localhost:8000/mcp",
      "auth": {
        "type": "oauth2",
        "authorization_url": "http://localhost:8000/oauth/authorize",
        "token_url": "http://localhost:8000/oauth/token",
        "client_id": "TU_CLIENT_ID_REGISTRADO",
        "scopes": ["mcp:read", "mcp:write"]
      }
    }
  }
}
```

Claude Desktop abrirá automáticamente el navegador para el login OAuth en la primera conexión.

---

## Desarrollo local (sin Docker)

Para iterar rápido en desarrollo puedes levantar solo Qdrant con Docker y los servicios Python directamente:

```bash
# 1. Levantar solo Qdrant
docker compose up qdrant -d

# 2. RAG Service
cd rag_service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
QDRANT_HOST=localhost uvicorn main:app --reload --port 8001

# 3. MCP Server (en otra terminal)
cd mcp_server
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
RAG_SERVICE_URL=http://localhost:8001 uvicorn main:app --reload --port 8000
```

---

## Documentación interactiva (Swagger)

Disponible en desarrollo:

- RAG Service: http://localhost:8001/docs
- MCP Server: http://localhost:8000/docs
- OAuth metadata: http://localhost:8000/.well-known/oauth-authorization-server

---

## Estructura del proyecto

```
mcp-rag-system/
├── .env.example                  # Plantilla de variables de entorno
├── docker-compose.yml            # Orquestación de servicios
├── credentials/
│   └── google_credentials.json   # Service Account JSON (no commitear)
│
├── agents/
│   └── agents.md                 # Contexto y reglas de seguridad (este proyecto)
│
├── mcp_server/
│   ├── main.py                   # FastMCP + ASGI OAuth middleware
│   ├── auth/
│   │   ├── oauth.py              # OAuth 2.0 completo (RFC 7591/7009/8414)
│   │   └── settings.py           # Config via pydantic-settings
│   └── tools/
│       └── rag_tools.py          # 5 herramientas MCP
│
└── rag_service/
    ├── main.py                   # FastAPI con todos los endpoints
    ├── embeddings/
    │   └── embedder.py           # sentence-transformers (local)
    ├── ingestion/
    │   ├── google_drive.py       # Drive API v3
    │   └── processor.py          # Extracción de texto + chunking
    └── retrieval/
        └── retriever.py          # Cliente Qdrant async
```

---

## Configuración avanzada

### Cambiar el modelo de embeddings

Edita `.env`:
```bash
EMBEDDING_MODEL=all-mpnet-base-v2          # Mayor calidad, 768 dims, más lento
# EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2  # Multilingüe
# EMBEDDING_MODEL=BAAI/bge-small-en-v1.5   # Excelente para retrieval en inglés
```

Reconstruye la imagen para pre-descargar el nuevo modelo:
```bash
docker compose build rag_service
docker compose up
```

### Cambiar tamaño de chunks

```bash
CHUNK_SIZE=1024     # Chunks más grandes, menos granularidad
CHUNK_OVERLAP=128
```

### Múltiples colecciones

Puedes tener colecciones separadas para distintos proyectos o clientes:

```bash
# Ingestar en una colección específica
curl -X POST http://localhost:8001/ingest/google-drive \
  -H "Content-Type: application/json" \
  -d '{"folder_id": "ID", "collection": "proyecto-alpha"}'

# Buscar solo en esa colección
curl -X POST http://localhost:8001/search \
  -H "Content-Type: application/json" \
  -d '{"query": "...", "collection": "proyecto-alpha"}'
```

---

## Resolución de problemas

### El RAG service tarda mucho en arrancar

Normal en el primer arranque: está descargando el modelo de embeddings. Espera hasta ver:
```
[RAG] Model loaded. Dimension: 384
```

### Error de conexión a Qdrant

```
qdrant_client.http.exceptions.UnexpectedResponse: ...
```

Verifica que Qdrant está corriendo:
```bash
docker compose ps
curl http://localhost:6333/health
```

### Error de Google Drive: "File not found" o "403 Forbidden"

- Verifica que el Service Account tiene acceso a la carpeta en Drive (compartir como Lector).
- Verifica que el fichero `credentials/google_credentials.json` existe y es válido.
- Comprueba que la Drive API está activada en tu proyecto de Google Cloud.

### El token OAuth da error 401 inmediatamente

- Verifica que `OAUTH_SECRET_KEY` en `.env` coincide con el valor con que se generó el token.
- El token puede haber expirado (60 minutos por defecto). Obtén uno nuevo.

### Los servicios no se ven entre sí en Docker

Todos los servicios están en la red `default` de Docker Compose. El MCP server se comunica con el RAG service usando el nombre del servicio: `http://rag_service:8001`. Esto es correcto dentro de Docker. Desde tu máquina local usa `localhost`.

---

## Seguridad en producción

Consulta el fichero [`agents/agents.md`](agents/agents.md) para la lista completa de reglas de seguridad. Resumen para producción:

1. **TLS obligatorio** — usa un reverse proxy (Nginx, Traefik, Caddy) con certificado válido.
2. **CORS restringido** — cambia `ALLOWED_ORIGINS` a tu dominio exacto.
3. **Deshabilita Swagger** — `app = FastAPI(docs_url=None, redoc_url=None)` en `rag_service/main.py`.
4. **No expongas Qdrant** — puertos `6333` y `6334` solo deben ser accesibles internamente.
5. **Clave OAuth fuerte** — mínimo 32 caracteres aleatorios.
6. **Redis para tokens OAuth** — reemplaza los dicts en memoria de `oauth.py`.

---

## Contribuir

1. Lee [`agents/agents.md`](agents/agents.md) antes de empezar — especialmente las reglas de seguridad y el checklist de PRs.
2. Crea una rama: `git checkout -b feat/mi-mejora`
3. Haz tus cambios respetando las convenciones de código.
4. Pasa el checklist de revisión del `agents.md`.
5. Abre un Pull Request con descripción del cambio y su impacto en seguridad.
