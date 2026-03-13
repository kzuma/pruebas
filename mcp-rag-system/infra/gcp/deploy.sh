#!/usr/bin/env bash
# =============================================================================
# deploy.sh — First-time GCP infrastructure setup + deployment
#
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - Docker installed and running
#   - Billing enabled on the project
#
# Usage:
#   ./infra/gcp/deploy.sh
#
# What it does:
#   1. Enables required GCP APIs
#   2. Creates Artifact Registry repository
#   3. Creates Secret Manager secrets
#   4. Creates Cloud Memorystore (Redis) instance
#   5. Creates Serverless VPC Connector
#   6. Creates Service Accounts with least-privilege IAM roles
#   7. Builds and pushes Docker images
#   8. Deploys rag-service (internal ingress)
#   9. Deploys mcp-server (public ingress) with rag-service URL
#  10. Grants Cloud Run invoker role between services
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — edit these before running
# ---------------------------------------------------------------------------
PROJECT_ID="your-gcp-project-id"
REGION="us-central1"
REPO_NAME="mcp-rag"
TAG="${TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"

# VPC network for Memorystore + VPC connector (use "default" or your VPC)
VPC_NETWORK="default"
VPC_CONNECTOR_NAME="mcp-rag-connector"
VPC_CONNECTOR_RANGE="10.8.0.0/28"   # /28 CIDR not used elsewhere in your VPC

# Redis (Cloud Memorystore)
REDIS_INSTANCE_NAME="mcp-rag-redis"
REDIS_TIER="BASIC"     # use STANDARD_HA for production failover
REDIS_SIZE=1           # GB

# Service accounts
SA_RAG="rag-service-sa"
SA_MCP="mcp-server-sa"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo ""; echo ">>> $*"; }
check() { command -v "$1" &>/dev/null || { echo "ERROR: '$1' not found"; exit 1; }; }

check gcloud
check docker

gcloud config set project "$PROJECT_ID"
gcloud config set run/region "$REGION"
AR_HOST="${REGION}-docker.pkg.dev"
IMAGE_RAG="${AR_HOST}/${PROJECT_ID}/${REPO_NAME}/rag-service:${TAG}"
IMAGE_MCP="${AR_HOST}/${PROJECT_ID}/${REPO_NAME}/mcp-server:${TAG}"

# ---------------------------------------------------------------------------
# 1. Enable APIs
# ---------------------------------------------------------------------------
info "Enabling GCP APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  redis.googleapis.com \
  vpcaccess.googleapis.com \
  aiplatform.googleapis.com \
  drive.googleapis.com \
  --quiet

# ---------------------------------------------------------------------------
# 2. Artifact Registry
# ---------------------------------------------------------------------------
info "Creating Artifact Registry repository '${REPO_NAME}'..."
gcloud artifacts repositories create "$REPO_NAME" \
  --repository-format=docker \
  --location="$REGION" \
  --quiet 2>/dev/null || echo "  (already exists, skipping)"

gcloud auth configure-docker "${AR_HOST}" --quiet

# ---------------------------------------------------------------------------
# 3. Secret Manager secrets
# ---------------------------------------------------------------------------
info "Creating secrets in Secret Manager..."

create_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "$name" &>/dev/null; then
    echo "  Secret '$name' exists — adding new version"
    echo -n "$value" | gcloud secrets versions add "$name" --data-file=-
  else
    echo -n "$value" | gcloud secrets create "$name" \
      --data-file=- \
      --replication-policy=automatic
  fi
}

# Prompt for sensitive values if not set as env vars
OAUTH_SECRET_KEY="${OAUTH_SECRET_KEY:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')}"

create_secret "oauth-secret-key"  "$OAUTH_SECRET_KEY"
create_secret "admin-username"    "$ADMIN_USERNAME"
create_secret "admin-password"    "$ADMIN_PASSWORD"

# Google Drive credentials
if [[ -f "credentials/google_credentials.json" ]]; then
  if gcloud secrets describe "google-drive-credentials" &>/dev/null; then
    gcloud secrets versions add "google-drive-credentials" \
      --data-file=credentials/google_credentials.json
  else
    gcloud secrets create "google-drive-credentials" \
      --data-file=credentials/google_credentials.json \
      --replication-policy=automatic
  fi
  echo "  Google Drive credentials uploaded to Secret Manager"
else
  echo "  WARNING: credentials/google_credentials.json not found."
  echo "  Create a placeholder secret and update it later:"
  echo -n "{}" | gcloud secrets create "google-drive-credentials" \
    --data-file=- --replication-policy=automatic 2>/dev/null || true
fi

# Qdrant Cloud credentials (set QDRANT_HOST and QDRANT_API_KEY env vars before running)
QDRANT_HOST="${QDRANT_HOST:-your-cluster.us-east-1-0.aws.cloud.qdrant.io}"
QDRANT_API_KEY="${QDRANT_API_KEY:-your-qdrant-api-key}"
create_secret "qdrant-host"    "$QDRANT_HOST"
create_secret "qdrant-api-key" "$QDRANT_API_KEY"

# ---------------------------------------------------------------------------
# 4. Cloud Memorystore (Redis)
# ---------------------------------------------------------------------------
info "Creating Cloud Memorystore Redis instance '${REDIS_INSTANCE_NAME}'..."
if gcloud redis instances describe "$REDIS_INSTANCE_NAME" \
     --region="$REGION" &>/dev/null; then
  echo "  Already exists, skipping"
else
  gcloud redis instances create "$REDIS_INSTANCE_NAME" \
    --size="$REDIS_SIZE" \
    --region="$REGION" \
    --tier="$REDIS_TIER" \
    --network="$VPC_NETWORK" \
    --redis-version=redis_7_0 \
    --quiet
fi

REDIS_IP=$(gcloud redis instances describe "$REDIS_INSTANCE_NAME" \
  --region="$REGION" \
  --format="value(host)")
REDIS_URL="redis://${REDIS_IP}:6379"
echo "  Redis IP: ${REDIS_IP}"
create_secret "redis-url" "$REDIS_URL"

# ---------------------------------------------------------------------------
# 5. Serverless VPC Connector
# ---------------------------------------------------------------------------
info "Creating VPC Serverless Connector '${VPC_CONNECTOR_NAME}'..."
if gcloud compute networks vpc-access connectors describe "$VPC_CONNECTOR_NAME" \
     --region="$REGION" &>/dev/null; then
  echo "  Already exists, skipping"
else
  gcloud compute networks vpc-access connectors create "$VPC_CONNECTOR_NAME" \
    --network="$VPC_NETWORK" \
    --region="$REGION" \
    --range="$VPC_CONNECTOR_RANGE" \
    --quiet
fi

CONNECTOR_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/connectors/${VPC_CONNECTOR_NAME}"

# ---------------------------------------------------------------------------
# 6. Service Accounts
# ---------------------------------------------------------------------------
info "Creating service accounts..."

create_sa() {
  local sa="$1" display="$2"
  gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" \
    &>/dev/null || \
  gcloud iam service-accounts create "$sa" \
    --display-name="$display" \
    --quiet
}

create_sa "$SA_RAG" "RAG Service"
create_sa "$SA_MCP" "MCP Server"

# RAG service roles
for role in \
  roles/aiplatform.user \
  roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_RAG}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$role" --quiet
done

# MCP server roles
for role in \
  roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_MCP}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$role" --quiet
done

# ---------------------------------------------------------------------------
# 7. Build and push Docker images
# ---------------------------------------------------------------------------
info "Building and pushing Docker images (tag: ${TAG})..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

docker build -t "$IMAGE_RAG" \
  --build-arg EMBEDDING_MODEL=all-MiniLM-L6-v2 \
  "${PROJECT_ROOT}/rag_service"
docker push "$IMAGE_RAG"

docker build -t "$IMAGE_MCP" "${PROJECT_ROOT}/mcp_server"
docker push "$IMAGE_MCP"

# ---------------------------------------------------------------------------
# 8. Deploy rag-service (internal)
# ---------------------------------------------------------------------------
info "Deploying rag-service..."
gcloud run deploy rag-service \
  --image="$IMAGE_RAG" \
  --region="$REGION" \
  --service-account="${SA_RAG}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --ingress=internal \
  --no-allow-unauthenticated \
  --timeout=120 \
  --concurrency=80 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=3 \
  --vpc-connector="$CONNECTOR_RESOURCE" \
  --vpc-egress=private-ranges-only \
  --set-secrets="QDRANT_HOST=qdrant-host:latest,QDRANT_API_KEY=qdrant-api-key:latest" \
  --set-secrets="/app/credentials/google_credentials.json=google-drive-credentials:latest" \
  --set-env-vars="QDRANT_PORT=6333,EMBEDDING_BACKEND=vertex_ai,VERTEX_AI_PROJECT=${PROJECT_ID},VERTEX_AI_LOCATION=${REGION},VERTEX_AI_EMBEDDING_MODEL=text-embedding-005,COLLECTION_NAME=documents,CHUNK_SIZE=512,CHUNK_OVERLAP=64" \
  --quiet

RAG_URL=$(gcloud run services describe rag-service \
  --region="$REGION" \
  --format="value(status.url)")
echo "  rag-service URL: ${RAG_URL}"

# ---------------------------------------------------------------------------
# 9. Deploy mcp-server (public)
# ---------------------------------------------------------------------------
info "Deploying mcp-server..."
gcloud run deploy mcp-server \
  --image="$IMAGE_MCP" \
  --region="$REGION" \
  --service-account="${SA_MCP}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --ingress=all \
  --allow-unauthenticated \
  --timeout=60 \
  --concurrency=200 \
  --memory=256Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=5 \
  --vpc-connector="$CONNECTOR_RESOURCE" \
  --vpc-egress=private-ranges-only \
  --set-secrets="OAUTH_SECRET_KEY=oauth-secret-key:latest,ADMIN_USERNAME=admin-username:latest,ADMIN_PASSWORD=admin-password:latest,REDIS_URL=redis-url:latest" \
  --set-env-vars="RAG_SERVICE_URL=${RAG_URL},CLOUD_RUN_ENV=true,GCP_PROJECT_ID=${PROJECT_ID},ALLOWED_ORIGINS=https://claude.ai,MCP_SERVER_NAME=rag-mcp-server" \
  --quiet

MCP_URL=$(gcloud run services describe mcp-server \
  --region="$REGION" \
  --format="value(status.url)")
echo "  mcp-server URL: ${MCP_URL}"

# ---------------------------------------------------------------------------
# 10. IAM: allow mcp-server SA to invoke rag-service
# ---------------------------------------------------------------------------
info "Granting Cloud Run invoker role to mcp-server on rag-service..."
gcloud run services add-iam-policy-binding rag-service \
  --region="$REGION" \
  --member="serviceAccount:${SA_MCP}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/run.invoker \
  --quiet

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "============================================================"
echo " Deployment complete"
echo "============================================================"
echo " MCP Server:  ${MCP_URL}"
echo " RAG Service: ${RAG_URL}  (internal only)"
echo ""
echo " OAuth metadata: ${MCP_URL}/.well-known/oauth-authorization-server"
echo " Admin user:     ${ADMIN_USERNAME}"
echo " Admin password: ${ADMIN_PASSWORD}"
echo ""
echo " Add to Claude Desktop claude_desktop_config.json:"
echo '  {'
echo '    "mcpServers": {'
echo '      "rag-knowledge-base": {'
echo "        \"url\": \"${MCP_URL}/mcp\","
echo '        "auth": { "type": "oauth2" }'
echo '      }'
echo '    }'
echo '  }'
echo "============================================================"
