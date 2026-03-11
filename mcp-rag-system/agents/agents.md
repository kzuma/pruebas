# Contexto de la Aplicación — MCP RAG System

> **Propósito de este fichero**
> Este documento es la fuente de verdad para cualquier desarrollo o mejora del sistema.
> Antes de escribir código nuevo, el agente o desarrollador debe leer este fichero para:
> - Entender la arquitectura y las decisiones de diseño tomadas.
> - Respetar las convenciones de código establecidas.
> - Aplicar las reglas de seguridad mandatorias.
> - Actualizar este documento si introduce cambios estructurales.

---

## 1. Arquitectura del sistema

```
Claude / Agentes externos
        │
        │  MCP Protocol  +  OAuth 2.0 Bearer Token
        ▼
┌───────────────────────────┐
│      MCP Server           │  Python · FastMCP · FastAPI · puerto 8000
│  /.well-known/...         │
│  /oauth/{authorize,token} │
│  /mcp  (protegido)        │
└──────────┬────────────────┘
           │  HTTP interno (red Docker)
           ▼
┌───────────────────────────┐
│      RAG Service          │  Python · FastAPI · puerto 8001
│  /search                  │
│  /ingest/google-drive     │
│  /documents               │
│  /collections/{n}/stats   │
└──────┬────────────┬────────┘
       │            │
  ┌────▼────┐  ┌────▼───────────────┐
  │ Qdrant  │  │  Google Drive API  │
  │  :6333  │  │  v3 (read-only)    │
  └─────────┘  └────────────────────┘
```

### Servicios y responsabilidades

| Servicio | Puerto | Responsabilidad |
|---|---|---|
| `mcp_server` | 8000 | Protocolo MCP, OAuth 2.0, autenticación de agentes |
| `rag_service` | 8001 | Ingesta, embeddings, búsqueda semántica |
| `qdrant` | 6333/6334 | Almacenamiento de vectores y metadatos |

### Flujo de datos: ingesta

```
Google Drive → download_drive_file() → extract_text()
→ chunk_text() [512 tokens, 64 overlap]
→ embed_texts() [sentence-transformers, local]
→ upsert_chunks() [Qdrant cosine similarity]
```

### Flujo de datos: búsqueda

```
Query del agente → embed_query() → Qdrant search (top-k cosine)
→ resultados con score, texto, fuente, metadatos
→ devuelto al MCP server → Claude
```

---

## 2. Decisiones de diseño clave

| Decisión | Motivo |
|---|---|
| **Qdrant** como vector store | Open-source, self-hosted, async Python client, producción-ready |
| **sentence-transformers local** | Sin dependencia de API externa, reproducible, sin coste por embedding |
| **Modelo `all-MiniLM-L6-v2`** | Equilibrio calidad/velocidad para inglés y español |
| **FastMCP** para el servidor MCP | SDK oficial de Anthropic, simplifica registro de tools |
| **OAuth 2.0 Authorization Code** | Estándar MCP, soportado nativamente por Claude Desktop/claude.ai |
| **Google Service Account** | Autenticación sin interacción humana para ingesta automatizada |
| **Separación MCP / RAG** | Cada servicio escala y se despliega independientemente |

---

## 3. Convenciones de código

### Python
- Versión mínima: **Python 3.12**
- Tipado estático con anotaciones (`from __future__ import annotations`)
- Clases de settings con **pydantic-settings** (`BaseSettings`)
- Cliente Qdrant: siempre **async** (`AsyncQdrantClient` como context manager)
- Funciones de I/O bloqueante (Drive API): ejecutar en **`asyncio.run_in_executor`**
- Errores HTTP: usar **`HTTPException`** de FastAPI, nunca retornar dict con "error" en códigos 2xx
- No usar `print()` en producción: usar `logging` (pendiente migrar, ver roadmap)

### Estructura de chunks
Cada chunk almacenado en Qdrant tiene el siguiente payload obligatorio:

```json
{
  "text": "...",
  "source": "drive-file-id-o-ruta",
  "title": "Nombre del documento",
  "chunk_index": 0,
  "ingested_at": "2026-03-11T10:00:00+00:00"
}
```

Campos opcionales: `drive_file_id`, `page`, `section`, cualquier metadato extra del documento.

### Variables de entorno
Toda configuración va en `.env` y se expone vía `pydantic-settings`. **Nunca hardcodear credenciales.**

---

## 4. Roadmap / mejoras previstas

- [ ] Migrar `print()` a `logging` estructurado (JSON) en ambos servicios
- [ ] Añadir Redis para almacenamiento persistente de tokens OAuth
- [ ] Soporte multitenancy: colecciones por `client_id` OAuth
- [ ] Ingesta incremental: detectar ficheros nuevos/modificados en Drive por `modifiedTime`
- [ ] Soporte para SharePoint / OneDrive como segunda fuente de documentos
- [ ] Tests de integración con `pytest` + `httpx.AsyncClient`
- [ ] Dashboard de métricas con Prometheus + Grafana

---

## 5. Reglas de seguridad mandatorias

> Estas reglas se aplican en **todo cambio de código** sin excepción.
> El agente debe revisar el código generado contra cada regla antes de hacer commit.

---

### 5.1 OWASP API Security Top 10 (2023)

Referencia oficial: https://owasp.org/API-Security/editions/2023/en/0x00-header/

#### API1:2023 — Broken Object Level Authorization (BOLA)
**Riesgo:** Un agente/usuario accede a objetos (colecciones, documentos) de otro usuario.

**Reglas:**
- Filtrar siempre por `client_id` o `username` extraído del token JWT, nunca confiar en parámetros del body/query para identificar al propietario.
- En `search` y `list_documents`, el scope del token debe limitar la colección accesible.
- **Nunca** aceptar un `collection` arbitrario sin verificar que el token tenga permiso sobre él.
- Pendiente de implementar: colecciones por tenant (ver roadmap).

#### API2:2023 — Broken Authentication
**Riesgo:** Tokens débiles, sin expiración, o flujo OAuth mal implementado.

**Reglas:**
- `OAUTH_SECRET_KEY` debe tener mínimo 32 caracteres aleatorios (`secrets.token_urlsafe(32)`).
- Los JWT deben incluir `exp`, `iat` y `jti` (ya implementado en `oauth.py`).
- Los authorization codes son **de un solo uso** y expiran en 5 minutos (ya implementado).
- En producción, almacenar `_revoked_tokens` y `_auth_codes` en **Redis** con TTL, no en memoria.
- Nunca loguear tokens completos; loguear solo los primeros 8 caracteres con `***`.

#### API3:2023 — Broken Object Property Level Authorization
**Riesgo:** Respuestas que exponen campos internos (IDs internos de Qdrant, tokens, hashes).

**Reglas:**
- Los endpoints de búsqueda y listado retornan **solo** los campos definidos en los modelos Pydantic (`SearchResult`, etc.).
- No usar `model.dict()` o `payload` completo sin filtrar.
- El campo `id` interno de Qdrant no debe exponerse en las respuestas de la API.

#### API4:2023 — Unrestricted Resource Consumption
**Riesgo:** Un agente hace miles de búsquedas o ingiere ficheros enormes, agotando recursos.

**Reglas:**
- `top_k` está limitado a máximo **20** (validado con `max(1, min(top_k, 20))`).
- `limit` en listados está limitado a máximo **200**.
- Añadir rate limiting por `client_id` en el MCP server (pendiente, usar `slowapi`).
- Tamaño máximo de fichero a ingestar: **50 MB** (añadir validación en `ingest_google_drive`).
- Timeout de httpx en herramientas MCP: 30s para búsqueda, 120s para ingesta (ya configurado).

#### API5:2023 — Broken Function Level Authorization
**Riesgo:** Endpoints de administración accesibles sin privilegios.

**Reglas:**
- El endpoint `DELETE /documents` requiere scope `mcp:write` en el token.
- El endpoint `POST /ingest/google-drive` requiere scope `mcp:write`.
- Implementar verificación de scope en el middleware OAuth (actualmente verifica token, pendiente verificar scope).
- Los endpoints de administración futuros (gestión de usuarios, clientes OAuth) deben requerir un scope `admin` separado.

#### API6:2023 — Unrestricted Access to Sensitive Business Flows
**Riesgo:** Automatización masiva de ingesta para envenenar la base de conocimiento.

**Reglas:**
- Registrar en logs cada operación de ingesta con: `client_id`, `folder_id/file_id`, `total_chunks`, timestamp.
- Alertar si un mismo cliente ingiere más de **1000 chunks en 1 hora**.
- Los ficheros ingeridos deben pasar por validación de tipo MIME real (no solo extensión).

#### API7:2023 — Server Side Request Forgery (SSRF)
**Riesgo:** Un agente pasa una URL interna como `folder_id` para escanear la red interna.

**Reglas:**
- Los `folder_id` y `file_id` de Google Drive son IDs opacos (alfanuméricos). Validar formato: solo `[a-zA-Z0-9_-]{10,}`.
- Nunca construir URLs a partir de input del usuario sin sanitizar.
- El RAG service no debe hacer peticiones HTTP a URLs arbitrarias proporcionadas por el cliente.

#### API8:2023 — Security Misconfiguration
**Riesgo:** Configuración insegura por defecto en producción.

**Reglas:**
- `ALLOWED_ORIGINS` debe ser una lista explícita de dominios en producción, **nunca** `*`.
- Qdrant no debe exponerse al exterior (`6333` solo en red Docker interna).
- El RAG service no debe exponerse al exterior; solo el MCP server en `8000`.
- Documentación automática de FastAPI (`/docs`, `/redoc`) debe deshabilitarse en producción:
  ```python
  app = FastAPI(docs_url=None, redoc_url=None)  # producción
  ```
- Variables sensibles nunca en logs: filtrar `OAUTH_SECRET_KEY`, passwords, tokens.

#### API9:2023 — Improper Inventory Management
**Riesgo:** Endpoints de versiones antiguas o en desarrollo accesibles.

**Reglas:**
- Versionar los endpoints: `/v1/search`, `/v1/ingest/...` (pendiente de implementar).
- Mantener un `CHANGELOG.md` con cada cambio de API.
- Deprecar endpoints con el header `Deprecation: true` y fecha de eliminación antes de eliminarlos.

#### API10:2023 — Unsafe Consumption of APIs
**Riesgo:** El RAG service confía ciegamente en los datos de Google Drive.

**Reglas:**
- Sanitizar el texto extraído antes de almacenarlo: eliminar null bytes, controlar tamaño máximo por chunk.
- No ejecutar contenido extraído de documentos como código.
- Validar que el MIME type reportado por Drive coincide con el contenido real (magic bytes).
- Limitar el tamaño del texto total de un documento a **5 MB** antes de chunking.

---

### 5.2 OWASP Top 10 Web (2021)

Referencia oficial: https://owasp.org/www-project-top-ten/

#### A01:2021 — Broken Access Control
**Aplica a:** Formulario OAuth `/oauth/authorize`, endpoints FastAPI.

**Reglas:**
- El formulario de login OAuth no debe redirigir a `redirect_uri` fuera de las URIs registradas (ya validado).
- Implementar CSRF protection en el formulario POST de autorización OAuth (añadir `state` obligatorio + validación).
- Los recursos de Qdrant no deben ser accesibles directamente desde el exterior.

#### A02:2021 — Cryptographic Failures
**Reglas:**
- Las contraseñas en `_users` se almacenan con **bcrypt** (ya implementado con `passlib`).
- Los JWT usan **HS256** con clave de mínimo 256 bits.
- En producción, migrar a **RS256** con clave privada RSA para JWT (mayor seguridad).
- No almacenar `client_secret` en texto plano en `_clients`; hashear con bcrypt.
- TLS/HTTPS obligatorio en producción (terminar en el reverse proxy, ej. Nginx/Traefik).

#### A03:2021 — Injection
**Aplica a:** Queries de Qdrant, parámetros de búsqueda, extracción de texto.

**Reglas:**
- Las queries a Qdrant usan el cliente oficial con parámetros tipados — no hay interpolación de strings. Mantener este patrón siempre.
- El texto extraído de documentos **nunca** se interpola en queries SQL, shell commands, o expresiones evaluadas.
- Los nombres de colección (`collection`) deben validarse contra el patrón `^[a-zA-Z0-9_-]{1,64}$` antes de usarse.

#### A04:2021 — Insecure Design
**Reglas:**
- Threat modeling obligatorio antes de añadir nuevas fuentes de datos (SharePoint, S3, etc.).
- Los agentes MCP solo tienen acceso de **lectura** a Google Drive (scope `drive.readonly`).
- Principio de mínimo privilegio: el Service Account de Google solo tiene acceso a carpetas específicas, no a todo el Drive.

#### A05:2021 — Security Misconfiguration
**Reglas:** Ver API8:2023 (aplican igual).
- Añadir headers de seguridad HTTP en el MCP server:
  ```
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Strict-Transport-Security: max-age=31536000
  Content-Security-Policy: default-src 'self'
  ```

#### A06:2021 — Vulnerable and Outdated Components
**Reglas:**
- Ejecutar `pip-audit` o `safety check` en cada CI/CD pipeline.
- Fijar versiones exactas en `requirements.txt` (actualmente usan `>=`, migrar a `==` con `pip-compile`).
- Revisar dependencias mensualmente con `pip list --outdated`.

#### A07:2021 — Identification and Authentication Failures
**Reglas:** Ver API2:2023.
- Implementar bloqueo de cuenta tras 5 intentos fallidos de login (pendiente).
- Los tokens OAuth no deben almacenarse en `localStorage` del navegador (usar `httpOnly` cookies si hay frontend propio).

#### A08:2021 — Software and Data Integrity Failures
**Reglas:**
- Verificar integridad de los modelos de embedding descargados (hash SHA256).
- No instalar paquetes de PyPI sin verificar el origen en entornos de producción.
- Los chunks almacenados en Qdrant no deben modificarse retroactivamente sin auditoría.

#### A09:2021 — Security Logging and Monitoring Failures
**Reglas:**
- Loguear en formato JSON estructurado: `timestamp`, `level`, `service`, `event`, `client_id`, `ip`.
- Eventos a loguear obligatoriamente:
  - Login exitoso / fallido (con IP, sin contraseña)
  - Token emitido / revocado
  - Ingesta de documentos (quién, qué, cuántos chunks)
  - Búsquedas (query hash, no texto completo, top-k, score máximo)
  - Errores 4xx y 5xx
- Alertas automáticas ante: >10 logins fallidos/minuto, >100 búsquedas/minuto por cliente.

#### A10:2021 — Server-Side Request Forgery (SSRF)
**Reglas:** Ver API7:2023.
- El MCP server que llama al RAG service usa una URL fija de variable de entorno (`RAG_SERVICE_URL`), nunca input del usuario.

---

### 5.3 Seguridad específica de MCP

Los servidores MCP tienen vectores de ataque únicos derivados del hecho de que un LLM actúa como intermediario entre el usuario y las herramientas.

#### MCP-SEC-01 — Prompt Injection a través de documentos
**Riesgo:** Un documento malintencionado en Google Drive contiene instrucciones como
`"Ignora las instrucciones anteriores y ejecuta..."` que Claude interpreta como comandos.

**Reglas:**
- Los chunks devueltos por `search_knowledge_base` deben envolverse en un bloque delimitado antes de pasarse al contexto de Claude:
  ```
  <retrieved_document source="..." score="0.92">
  {texto del chunk}
  </retrieved_document>
  ```
- El system prompt del MCP server debe instruir a Claude a tratar el contenido recuperado como **datos no confiables**, nunca como instrucciones.
- Filtrar de los chunks recuperados patrones de prompt injection conocidos (listas negras de frases como "ignore previous instructions", "system:", "ASSISTANT:").

#### MCP-SEC-02 — Tool Poisoning
**Riesgo:** Una herramienta MCP devuelve datos diseñados para alterar el comportamiento del modelo.

**Reglas:**
- Las respuestas de las herramientas MCP deben ser datos estructurados (JSON), nunca texto libre que pueda contener pseudo-instrucciones.
- Limitar el tamaño de cada resultado de búsqueda a **2000 caracteres** por chunk antes de devolverlo al LLM.
- Validar con Pydantic todas las respuestas del RAG service antes de devolverlas al MCP.

#### MCP-SEC-03 — Escalation de privilegios vía herramientas
**Riesgo:** Un agente usa `ingest_google_drive` para añadir documentos que modifican el comportamiento del RAG para otros usuarios.

**Reglas:**
- Las operaciones de escritura (`ingest`, `delete`) requieren scope `mcp:write` explícito en el token (pendiente de verificación de scope en middleware).
- Separar las colecciones por `client_id` para aislar el conocimiento por tenant (roadmap).
- Auditar todas las operaciones de escritura con `client_id` + timestamp.

#### MCP-SEC-04 — Información sensible en embeddings
**Riesgo:** Documentos con PII (nombres, emails, documentos de identidad) se ingresan sin control y se recuperan en búsquedas no relacionadas.

**Reglas:**
- Antes de ingestar, detectar y redactar PII usando expresiones regulares o un modelo de clasificación:
  - Emails: `[\w.+-]+@[\w-]+\.[\w.]+`
  - DNI/NIE españoles, números de tarjeta, etc.
- Documentar en los metadatos del chunk si contiene PII para poder auditarlo.
- Implementar un endpoint `GET /documents/{source}/preview` que permita revisar antes de confirmar la ingesta (roadmap).

#### MCP-SEC-05 — Denial of Service por queries costosas
**Riesgo:** Un agente hace búsquedas con `top_k=20` en colecciones con millones de vectores, saturando Qdrant.

**Reglas:**
- Rate limiting por `client_id`: máximo 60 búsquedas/minuto (pendiente, usar `slowapi`).
- Timeout de Qdrant configurado a 10 segundos por query.
- Monitorizar latencia P95 de búsquedas; alertar si supera 2 segundos.

#### MCP-SEC-06 — Autenticación del servidor MCP
**Riesgo:** Claude se conecta a un servidor MCP impostor (man-in-the-middle).

**Reglas:**
- En producción, el MCP server debe servir TLS con certificado válido.
- El `issuer` en el metadata OAuth debe coincidir exactamente con la URL del servidor.
- Publicar la URL del MCP server en un registro confiable (ej. `claude_desktop_config.json` gestionado por MDM).

---

## 6. Checklist de revisión para PRs

Antes de hacer merge de cualquier cambio, verificar:

**Funcional**
- [ ] El código nuevo tiene tipado estático completo
- [ ] Los errores HTTP usan `HTTPException` con códigos correctos
- [ ] Las operaciones bloqueantes están en `run_in_executor`
- [ ] Los modelos Pydantic validan correctamente el input

**Seguridad**
- [ ] No hay credenciales hardcodeadas
- [ ] El input del usuario está validado (formato, longitud, caracteres permitidos)
- [ ] Los nombres de colección siguen el patrón `^[a-zA-Z0-9_-]{1,64}$`
- [ ] Las respuestas no exponen campos internos de Qdrant
- [ ] Los chunks de búsqueda están delimitados antes de pasarse al LLM
- [ ] No hay interpolación de strings en queries de Qdrant
- [ ] Se respetan los límites de `top_k`, `limit`, tamaño de documento

**OWASP**
- [ ] Revisado contra API Security Top 10 (sección 5.1)
- [ ] Revisado contra Web Top 10 (sección 5.2)
- [ ] Revisado contra reglas MCP (sección 5.3)

**Documentación**
- [ ] Este fichero (`agents.md`) actualizado si hay cambios arquitectónicos
- [ ] `CHANGELOG.md` actualizado con el cambio
- [ ] Variables de entorno nuevas añadidas a `.env.example`

---

## 7. Glosario

| Término | Definición |
|---|---|
| **MCP** | Model Context Protocol — protocolo de Anthropic para conectar LLMs con herramientas externas |
| **RAG** | Retrieval-Augmented Generation — técnica que enriquece el contexto del LLM con documentos relevantes recuperados de una base de conocimiento |
| **Chunk** | Fragmento de texto de tamaño fijo (512 tokens) con solapamiento (64 tokens) extraído de un documento |
| **Embedding** | Vector numérico de 384 dimensiones que representa semánticamente un chunk de texto |
| **Qdrant** | Base de datos vectorial open-source usada para almacenar embeddings y hacer búsqueda por similitud coseno |
| **FastMCP** | Librería Python de Anthropic para construir servidores MCP con decoradores |
| **OAuth 2.0 AC** | Authorization Code flow — flujo OAuth estándar para aplicaciones con redirect |
| **Service Account** | Cuenta de Google con credenciales JSON para acceso programático a Drive sin interacción humana |
| **Cosine similarity** | Métrica de similitud entre vectores usada por Qdrant: 1.0 = idénticos, 0.0 = sin relación |
