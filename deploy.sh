#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Production deployment for the GoFormz extractor as an Azure Container Apps
# scheduled JOB (every Friday 07:00 UTC).
#
# Hardening vs. the original script:
#   * Stable, idempotent resource names (no $RANDOM).
#   * Immutable image tag (git sha / UTC timestamp), never :latest.
#   * ACR admin account NOT used — the Job pulls with a managed identity
#     that is granted only AcrPull.
#   * All sensitive values are Container App SECRETS (secretref:), sourced
#     from your shell/CI environment or Key Vault — never inlined here.
#   * Idempotent: re-running updates the existing Job instead of recreating.
#
# Required environment variables BEFORE running (export or set in CI):
#   GOFORMZ_EMAIL, GOFORMZ_PASSWORD
#   AZURE_DI_ENDPOINT, AZURE_DI_KEY
#   TENANT_ID, CLIENT_ID, CLIENT_SECRET
#   FABRIC_WORKSPACE_NAME, FABRIC_LAKEHOUSE_NAME
# ---------------------------------------------------------------------------
set -euo pipefail

# ---- Stable configuration (safe to keep in source) ------------------------
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-goformz}"
LOCATION="${LOCATION:-eastus}"
ACR_NAME="${ACR_NAME:-goformzacrprod}"          # globally unique, lowercase
ENVIRONMENT="${ENVIRONMENT:-goformz-env}"
JOB_NAME="${JOB_NAME:-goformz-extractor}"
IDENTITY_NAME="${IDENTITY_NAME:-id-goformz-job}"
IMAGE_REPO="goformz-extractor"
CRON="${CRON:-0 7 * * 5}"                       # Friday 07:00 UTC
CPU="${CPU:-1.0}"
MEMORY="${MEMORY:-2.0Gi}"
REPLICA_TIMEOUT="${REPLICA_TIMEOUT:-3600}"

# ---- Immutable image tag --------------------------------------------------
IMAGE_VERSION="$(git rev-parse --short HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)"
IMAGE_TAG="${IMAGE_REPO}:${IMAGE_VERSION}"

# ---- Validate required secrets are present in the environment -------------
require_var() {
  if [[ -z "${!1:-}" ]]; then
    echo "ERROR: required environment variable '$1' is not set." >&2
    exit 1
  fi
}
for v in GOFORMZ_EMAIL GOFORMZ_PASSWORD AZURE_DI_ENDPOINT AZURE_DI_KEY \
         TENANT_ID CLIENT_ID CLIENT_SECRET \
         FABRIC_WORKSPACE_NAME FABRIC_LAKEHOUSE_NAME; do
  require_var "$v"
done

echo ">> Registering providers / extensions"
az extension add --name containerapp --upgrade --only-show-errors
az provider register --namespace Microsoft.App --wait
az provider register --namespace Microsoft.OperationalInsights --wait
az provider register --namespace Microsoft.ContainerRegistry --wait

echo ">> Resource group"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none

echo ">> Azure Container Registry (admin DISABLED)"
az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" \
  --sku Basic --admin-enabled false -o none
ACR_LOGIN_SERVER=$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)
ACR_ID=$(az acr show -n "$ACR_NAME" --query id -o tsv)

echo ">> Build image in ACR (no local Docker needed): $IMAGE_TAG"
az acr build --registry "$ACR_NAME" --image "$IMAGE_TAG" . -o none
# Resolve the immutable digest for a fully reproducible deployment.
IMAGE_DIGEST=$(az acr repository show \
  --name "$ACR_NAME" --image "$IMAGE_TAG" --query digest -o tsv)
IMAGE_REF="${ACR_LOGIN_SERVER}/${IMAGE_REPO}@${IMAGE_DIGEST}"
echo "   Deploying image: $IMAGE_REF"

echo ">> User-assigned managed identity"
az identity create --resource-group "$RESOURCE_GROUP" \
  --name "$IDENTITY_NAME" -o none
IDENTITY_ID=$(az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" \
  --query id -o tsv)
IDENTITY_PRINCIPAL=$(az identity show -g "$RESOURCE_GROUP" -n "$IDENTITY_NAME" \
  --query principalId -o tsv)

echo ">> Grant AcrPull to the identity"
az role assignment create \
  --assignee-object-id "$IDENTITY_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "AcrPull" --scope "$ACR_ID" -o none || true

echo ">> Container Apps environment"
az containerapp env create --name "$ENVIRONMENT" \
  --resource-group "$RESOURCE_GROUP" --location "$LOCATION" -o none

# ---- Create or update the scheduled Job (idempotent) ----------------------
COMMON_SECRETS=(
  goformz-password="$GOFORMZ_PASSWORD"
  di-key="$AZURE_DI_KEY"
  fabric-client-secret="$CLIENT_SECRET"
)
COMMON_ENV=(
  GOFORMZ_EMAIL="$GOFORMZ_EMAIL"
  GOFORMZ_PASSWORD=secretref:goformz-password
  AZURE_DI_ENDPOINT="$AZURE_DI_ENDPOINT"
  AZURE_DI_KEY=secretref:di-key
  TENANT_ID="$TENANT_ID"
  CLIENT_ID="$CLIENT_ID"
  CLIENT_SECRET=secretref:fabric-client-secret
  FABRIC_WORKSPACE_NAME="$FABRIC_WORKSPACE_NAME"
  FABRIC_LAKEHOUSE_NAME="$FABRIC_LAKEHOUSE_NAME"
  REQUIRE_ONELAKE_UPLOAD=true
  CHROME_BIN=/usr/bin/chromium
  CHROMEDRIVER_PATH=/usr/bin/chromedriver
)

if az containerapp job show -n "$JOB_NAME" -g "$RESOURCE_GROUP" \
    -o none 2>/dev/null; then
  echo ">> Updating existing Job"
  az containerapp job update --name "$JOB_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --image "$IMAGE_REF" \
    --cpu "$CPU" --memory "$MEMORY" \
    --replica-timeout "$REPLICA_TIMEOUT" \
    --set-secrets "${COMMON_SECRETS[@]}" \
    --replace-env-vars "${COMMON_ENV[@]}" -o none
else
  echo ">> Creating scheduled Job ($CRON)"
  az containerapp job create --name "$JOB_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --environment "$ENVIRONMENT" \
    --trigger-type "Schedule" \
    --cron-expression "$CRON" \
    --replica-timeout "$REPLICA_TIMEOUT" \
    --replica-retry-limit 1 \
    --parallelism 1 \
    --replica-completion-count 1 \
    --image "$IMAGE_REF" \
    --cpu "$CPU" --memory "$MEMORY" \
    --mi-user-assigned "$IDENTITY_ID" \
    --registry-server "$ACR_LOGIN_SERVER" \
    --registry-identity "$IDENTITY_ID" \
    --secrets "${COMMON_SECRETS[@]}" \
    --env-vars "${COMMON_ENV[@]}" -o none
fi

echo ">> Done. Trigger a manual test run with:"
echo "   az containerapp job start -n $JOB_NAME -g $RESOURCE_GROUP"
echo ">> Watch executions with:"
echo "   az containerapp job execution list -n $JOB_NAME -g $RESOURCE_GROUP -o table"
