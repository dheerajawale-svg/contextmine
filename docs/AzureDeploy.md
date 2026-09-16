# Deploy ContextMine to Azure Container Apps

This runbook deploys the current ContextMine stack as three Azure Container
Apps backed by Azure Database for PostgreSQL Flexible Server, Azure Files, Azure
Container Registry (ACR), and Azure Key Vault.

```text
Internet -> contextmine-api (external HTTPS ingress, port 8000)
                |
                +-> PostgreSQL Flexible Server
                |     +- contextmine database (vector + AGE)
                |     +- prefect database (vector + AGE)
                |
                +-> contextmine-prefect (internal ingress, port 4200)
                         ^
contextmine-worker (no ingress, Azure Files at /data) -+
```

The API image serves the React UI, REST API, and MCP endpoint. Do not deploy a
separate web Container App. The worker does crawling, repository indexing, and
Prefect flow execution.

## 1. Architecture requirements

| Component | Azure resource | Required setup |
| --- | --- | --- |
| API | External Container App | HTTPS ingress to target port `8000` |
| Prefect | Internal Container App | Internal ingress to target port `4200`; one replica |
| Worker | Container App without ingress | One replica; Azure Files mounted at `/data` |
| Application data | PostgreSQL Flexible Server | `contextmine` and `prefect` databases, each with `vector` and `age` |
| Images | ACR | Immutable API and worker image tags |
| Credentials | Key Vault | Key Vault references accessed with a managed identity |

Do **not** deploy the WSL `pg4ai` database container to Azure Container Apps.
Flexible Server replaces it. Likewise, WSL aliases such as
`contextmine-prefect:4200` are not Container Apps service names. On Azure the
API and worker use the Prefect app's internal FQDN:

```text
http://contextmine-prefect.<container-apps-environment-default-domain>/api
```

### Production safety constraints

`APP_MODE=production` rejects unsafe configuration. Set all of the following:

- `DEBUG=false`.
- External HTTPS values for `PUBLIC_BASE_URL` and `MCP_OAUTH_BASE_URL`.
- Non-empty `CORS_ALLOWED_ORIGINS` and `MCP_ALLOWED_ORIGINS`.
- `SCIP_INSTALL_DEPS_MODE=never`.
- `SANDBOX_API_URL`, `SANDBOX_API_KEY`, and `SANDBOX_ANALYZER_SNAPSHOT`.

`MODEL_CALLS_ENABLED=false` is supported and gives deterministic extraction and
full-text-only retrieval without external embedding or LLM calls. It does **not**
remove the production sandbox requirement.

Keep Prefect and worker at one replica. The worker owns scheduler activity and
uses shared checkout/cache storage. Also keep the API at one replica while its
image entrypoint runs Alembic migrations at startup; use a separately managed
migration job before enabling API scale-out.

## 2. Prerequisites and network design

Install Azure CLI, sign in to the target subscription, and register providers:

```bash
az login
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.DBforPostgreSQL
az provider register --namespace Microsoft.KeyVault
```

Use a Flexible Server version and Azure region that permits **both** `VECTOR`
and `AGE`. Before deploying the application, confirm that the server accepts
`VECTOR,AGE` for `azure.extensions` and `AGE` for
`shared_preload_libraries`. If Azure rejects either parameter, stop: this
deployment cannot meet ContextMine's graph requirements in that region/version.

For production, use Flexible Server private access. Put the Container Apps
environment in a VNet infrastructure subnet and Flexible Server in a separate
subnet delegated to `Microsoft.DBforPostgreSQL/flexibleServers`. Link the
Flexible Server private DNS zone to the VNet. Do not use the Container Apps
subnet for the database. A public-access server may be used only for a
short-lived proof of concept with restricted firewall access.

## 3. Define non-secret variables

Use a Bash-compatible shell. Substitute organization-standard names and region.
No command in this section contains a secret.

```bash
export LOCATION="centralindia"
export RG="rg-contextmine-prod"
export ACR="<globally-unique-acr-name>"
export ENV="cae-contextmine-prod"
export KV="kv-contextmine-prod"
export IDENTITY="id-contextmine-aca"
export STORAGE="stcontextmineprod"
export SHARE="worker-data"
export PG_SERVER="pg-contextmine-prod"
export API_APP="contextmine-api"
export PREFECT_APP="contextmine-prefect"
export WORKER_APP="contextmine-worker"
```

## 4. Create foundation resources

Create the resource group, observability workspace, ACR, Key Vault, and a
user-assigned managed identity. Key Vault RBAC is used instead of access
policies so the same identity can be attached to all three apps.

```bash
az group create --name "$RG" --location "$LOCATION"
az monitor log-analytics workspace create --resource-group "$RG" \
  --workspace-name law-contextmine-prod --location "$LOCATION"
az acr create --resource-group "$RG" --name "$ACR" --sku Standard
az keyvault create --resource-group "$RG" --name "$KV" --location "$LOCATION" \
  --enable-rbac-authorization true
az identity create --resource-group "$RG" --name "$IDENTITY" --location "$LOCATION"

export ACR_ID="$(az acr show -g "$RG" -n "$ACR" --query id -o tsv)"
export KV_ID="$(az keyvault show -n "$KV" --query id -o tsv)"
export IDENTITY_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query id -o tsv)"
export IDENTITY_PRINCIPAL_ID="$(az identity show -g "$RG" -n "$IDENTITY" --query principalId -o tsv)"
export ACR_LOGIN_SERVER="$(az acr show -g "$RG" -n "$ACR" --query loginServer -o tsv)"

az role assignment create --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal --role AcrPull --scope "$ACR_ID"
az role assignment create --assignee-object-id "$IDENTITY_PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User" \
  --scope "$KV_ID"
```

Create the environment. Add the validated
`--infrastructure-subnet-resource-id` when using the recommended VNet design.

```bash
export LAW_ID="$(az monitor log-analytics workspace show -g "$RG" \
  -n law-contextmine-prod --query customerId -o tsv)"
export LAW_KEY="$(az monitor log-analytics workspace get-shared-keys -g "$RG" \
  -n law-contextmine-prod --query primarySharedKey -o tsv)"
az containerapp env create --name "$ENV" --resource-group "$RG" \
  --location "$LOCATION" --logs-workspace-id "$LAW_ID" --logs-workspace-key "$LAW_KEY"

export ENV_ID="$(az containerapp env show -g "$RG" -n "$ENV" --query id -o tsv)"
export ENV_DEFAULT_DOMAIN="$(az containerapp env show -g "$RG" -n "$ENV" \
  --query properties.defaultDomain -o tsv)"
```

## 5. Create and prepare Flexible Server

Create the Flexible Server through approved IaC, the portal, or Azure CLI. A
temporary public-access CLI example is below; private access must instead use
the previously created delegated subnet and private DNS zone.

```bash
az postgres flexible-server create --resource-group "$RG" --name "$PG_SERVER" \
  --location "$LOCATION" --tier Burstable --sku-name Standard_B2s \
  --storage-size 64 --version 16 --public-access <administrator-ip>

az postgres flexible-server parameter set --resource-group "$RG" --server-name "$PG_SERVER" \
  --name azure.extensions --value "VECTOR,AGE"
az postgres flexible-server parameter set --resource-group "$RG" --server-name "$PG_SERVER" \
  --name shared_preload_libraries --value "AGE"
export PG_HOST="$(az postgres flexible-server show -g "$RG" -n "$PG_SERVER" \
  --query fullyQualifiedDomainName -o tsv)"
```

Restart the server if Azure indicates that the preload setting is pending. From
a secure administrator `psql` session, create a dedicated application role and
the two databases. Do not place the actual password in source control, shell
history, or this document.

```sql
CREATE ROLE contextmine LOGIN PASSWORD '<strong-application-password>';
CREATE DATABASE contextmine OWNER contextmine;
CREATE DATABASE prefect OWNER contextmine;
```

Connect as an administrator to **each** database and run:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector', 'age');
```

The application requires asyncpg URLs, port `5432`, URL-encoded credentials,
and TLS:

```text
postgresql+asyncpg://contextmine:<url-encoded-password>@<server-fqdn>:5432/contextmine?ssl=require
postgresql+asyncpg://contextmine:<url-encoded-password>@<server-fqdn>:5432/prefect?ssl=require
```

## 6. Populate Key Vault

Create the following secrets through the approved secret workflow (portal, CI
secret injection, or a protected local file). Generate separate random values
for session and token encryption secrets with `python -c "import secrets;
print(secrets.token_urlsafe(32))"`.

| Key Vault name | Environment variable | Consumer |
| --- | --- | --- |
| `contextmine-database-url` | `DATABASE_URL` | API, worker |
| `prefect-database-url` | `PREFECT_API_DATABASE_CONNECTION_URL` | Prefect |
| `github-client-id` | `GITHUB_CLIENT_ID` | API |
| `github-client-secret` | `GITHUB_CLIENT_SECRET` | API |
| `session-secret` | `SESSION_SECRET` | API, worker |
| `token-encryption-key` | `TOKEN_ENCRYPTION_KEY` | API, worker |
| `sandbox-api-key` | `SANDBOX_API_KEY` | API, worker |
| provider key | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY` | API, worker when model calls are enabled |

For example, use a protected file rather than passing a secret in command-line
history:

```bash
az keyvault secret set --vault-name "$KV" --name contextmine-database-url \
  --file /secure/path/contextmine-database-url.txt
```

Repeat for each applicable secret. Do not store non-secret configuration such as
`SANDBOX_API_URL`, model mode, or origin allow-lists in Key Vault.

## 7. Build and publish immutable images

Build from the repository root. The worker's final Dockerfile stage is the slim
orchestration worker and requires the root build context.

```bash
export IMAGE_TAG="sha-$(git rev-parse --short=12 HEAD)"
az acr login --name "$ACR"
docker build -t "$ACR_LOGIN_SERVER/contextmine-api:$IMAGE_TAG" -f apps/api/Dockerfile .
docker build -t "$ACR_LOGIN_SERVER/contextmine-worker:$IMAGE_TAG" -f apps/worker/Dockerfile .
docker push "$ACR_LOGIN_SERVER/contextmine-api:$IMAGE_TAG"
docker push "$ACR_LOGIN_SERVER/contextmine-worker:$IMAGE_TAG"
```

## 8. Register Azure Files environment storage

The worker persists repositories and CPG artifacts in `/data/repos` and
`/data/joern-cpg`. Register an Azure Files share named `worker-data` with the
Container Apps environment; it will later be mounted at `/data`.

```bash
az storage account create --resource-group "$RG" --name "$STORAGE" \
  --location "$LOCATION" --sku Standard_LRS --kind StorageV2
az storage share-rm create --resource-group "$RG" --storage-account "$STORAGE" \
  --name "$SHARE" --quota 100

export STORAGE_KEY="$(az storage account keys list -g "$RG" --account-name "$STORAGE" \
  --query '[0].value' -o tsv)"
az containerapp env storage set --name "$ENV" --resource-group "$RG" \
  --storage-name worker-data --access-mode ReadWrite \
  --azure-file-account-name "$STORAGE" --azure-file-account-key "$STORAGE_KEY" \
  --azure-file-share-name "$SHARE"
```

The initial `100` GiB quota is a sizing decision, not a requirement. Monitor
capacity and IOPS as retained repositories and crawl artifacts grow.

## 9. Deploy Prefect first

Prefect is deployed before the API and worker because they submit flows to it.
Keep its ingress internal and set its UI API URL to the internal FQDN. Container
Apps ingress proxies HTTP to target port `4200`, so the URL contains no `:4200`.

```bash
az containerapp create --name "$PREFECT_APP" --resource-group "$RG" \
  --environment "$ENV" --image prefecthq/prefect:3.8.4-python3.14 \
  --user-assigned "$IDENTITY_ID" \
  --ingress internal --target-port 4200 --min-replicas 1 --max-replicas 1 \
  --secrets "prefect-database-url=keyvaultref:https://$KV.vault.azure.net/secrets/prefect-database-url,identityref:$IDENTITY_ID" \
  --env-vars PREFECT_SERVER_API_HOST=0.0.0.0 \
    "PREFECT_UI_API_URL=http://$PREFECT_APP.$ENV_DEFAULT_DOMAIN/api" \
    PREFECT_API_DATABASE_CONNECTION_URL=secretref:prefect-database-url \
  --command prefect --args server start --host 0.0.0.0
```

Check its logs before continuing:

```bash
az containerapp logs show --name "$PREFECT_APP" --resource-group "$RG" --tail 100
```

## 10. Deploy the external API

The standard Container Apps public FQDN is predictable from the app name and
environment default domain. Use it initially or replace `PUBLIC_URL` with a
pre-provisioned custom HTTPS host. The same final host must be used for GitHub
OAuth and MCP settings.

```bash
export PUBLIC_URL="https://$API_APP.$ENV_DEFAULT_DOMAIN"

az containerapp create --name "$API_APP" --resource-group "$RG" --environment "$ENV" \
  --image "$ACR_LOGIN_SERVER/contextmine-api:$IMAGE_TAG" \
  --user-assigned "$IDENTITY_ID" --registry-server "$ACR_LOGIN_SERVER" \
  --registry-identity "$IDENTITY_ID" \
  --ingress external --target-port 8000 --min-replicas 1 --max-replicas 1 \
  --secrets \
    "contextmine-database-url=keyvaultref:https://$KV.vault.azure.net/secrets/contextmine-database-url,identityref:$IDENTITY_ID" \
    "github-client-id=keyvaultref:https://$KV.vault.azure.net/secrets/github-client-id,identityref:$IDENTITY_ID" \
    "github-client-secret=keyvaultref:https://$KV.vault.azure.net/secrets/github-client-secret,identityref:$IDENTITY_ID" \
    "session-secret=keyvaultref:https://$KV.vault.azure.net/secrets/session-secret,identityref:$IDENTITY_ID" \
    "token-encryption-key=keyvaultref:https://$KV.vault.azure.net/secrets/token-encryption-key,identityref:$IDENTITY_ID" \
    "sandbox-api-key=keyvaultref:https://$KV.vault.azure.net/secrets/sandbox-api-key,identityref:$IDENTITY_ID" \
  --env-vars APP_MODE=production DEBUG=false DATABASE_URL=secretref:contextmine-database-url \
    GITHUB_CLIENT_ID=secretref:github-client-id GITHUB_CLIENT_SECRET=secretref:github-client-secret \
    SESSION_SECRET=secretref:session-secret TOKEN_ENCRYPTION_KEY=secretref:token-encryption-key \
    "PREFECT_API_URL=http://$PREFECT_APP.$ENV_DEFAULT_DOMAIN/api" \
    "PUBLIC_BASE_URL=$PUBLIC_URL" "MCP_OAUTH_BASE_URL=$PUBLIC_URL" \
    "CORS_ALLOWED_ORIGINS=$PUBLIC_URL" "MCP_ALLOWED_ORIGINS=https://<allowed-mcp-client-origin>" \
    SCIP_INSTALL_DEPS_MODE=never SANDBOX_API_URL=https://<sandbox-api-host> \
    SANDBOX_API_KEY=secretref:sandbox-api-key SANDBOX_ANALYZER_SNAPSHOT=<published-analyzer-snapshot> \
    MODEL_CALLS_ENABLED=false
```

Replace the angle-bracketed settings before running the command. With model
calls enabled, reference the selected provider Key Vault secret and add the
corresponding model configuration, for example
`OPENAI_API_KEY=secretref:openai-api-key` and
`DEFAULT_EMBEDDING_MODEL=openai:text-embedding-3-small`. With model calls
disabled, external provider keys are not required.

Register the GitHub OAuth callback exactly as:

```text
https://<final-public-host>/api/auth/callback
```

When changing to a custom domain, update the app custom-domain binding and
certificate, GitHub callback, `PUBLIC_BASE_URL`, `MCP_OAUTH_BASE_URL`, and CORS
origin in one controlled revision.

## 11. Deploy the worker with Azure Files

Use a declarative YAML file for the volume because it makes the `/data` mount
explicit. Create `worker.yaml` outside source control, substitute all
angle-bracketed values, and retain exactly one worker replica.

```yaml
name: contextmine-worker
resourceGroup: rg-contextmine-prod
location: centralindia
identity:
  type: UserAssigned
  userAssignedIdentities:
    <user-assigned-identity-resource-id>: {}
properties:
  managedEnvironmentId: <container-apps-environment-resource-id>
  configuration:
    registries:
      - server: <acr-login-server>
        identity: <user-assigned-identity-resource-id>
    secrets:
      - name: contextmine-database-url
        keyVaultUrl: https://<key-vault-name>.vault.azure.net/secrets/contextmine-database-url
        identity: <user-assigned-identity-resource-id>
      - name: session-secret
        keyVaultUrl: https://<key-vault-name>.vault.azure.net/secrets/session-secret
        identity: <user-assigned-identity-resource-id>
      - name: token-encryption-key
        keyVaultUrl: https://<key-vault-name>.vault.azure.net/secrets/token-encryption-key
        identity: <user-assigned-identity-resource-id>
      - name: sandbox-api-key
        keyVaultUrl: https://<key-vault-name>.vault.azure.net/secrets/sandbox-api-key
        identity: <user-assigned-identity-resource-id>
  template:
    containers:
      - name: worker
        image: <acr-login-server>/contextmine-worker:<immutable-image-tag>
        env:
          - { name: APP_MODE, value: production }
          - { name: DEBUG, value: "false" }
          - { name: DATABASE_URL, secretRef: contextmine-database-url }
          - { name: SESSION_SECRET, secretRef: session-secret }
          - { name: TOKEN_ENCRYPTION_KEY, secretRef: token-encryption-key }
          - { name: PREFECT_API_URL, value: http://contextmine-prefect.<environment-default-domain>/api }
          - { name: PREFECT_DUE_INTERVAL_SECONDS, value: "300" }
          - { name: PREFECT_WORKER_LIMIT, value: "2" }
          - { name: SCIP_INSTALL_DEPS_MODE, value: never }
          - { name: PUBLIC_BASE_URL, value: https://<final-public-host> }
          - { name: MCP_OAUTH_BASE_URL, value: https://<final-public-host> }
          - { name: CORS_ALLOWED_ORIGINS, value: https://<final-public-host> }
          - { name: MCP_ALLOWED_ORIGINS, value: https://<allowed-mcp-client-origin> }
          - { name: SANDBOX_API_URL, value: https://<sandbox-api-host> }
          - { name: SANDBOX_API_KEY, secretRef: sandbox-api-key }
          - { name: SANDBOX_ANALYZER_SNAPSHOT, value: <published-analyzer-snapshot> }
          - { name: MODEL_CALLS_ENABLED, value: "false" }
          - { name: OTEL_ENABLED, value: "false" }
          - { name: OTEL_SDK_DISABLED, value: "true" }
        volumeMounts:
          - { volumeName: worker-data, mountPath: /data }
    volumes:
      - { name: worker-data, storageType: AzureFile, storageName: worker-data }
    scale:
      minReplicas: 1
      maxReplicas: 1
```

Deploy and inspect the worker:

```bash
az containerapp create --name "$WORKER_APP" --resource-group "$RG" --yaml worker.yaml
az containerapp logs show --name "$WORKER_APP" --resource-group "$RG" --tail 100
```

When model calls are enabled, add the provider secret reference and model
settings to **both** API and worker. The worker executes the sync flows and
requires the same database, Prefect, public/security, sandbox, and model
configuration. The shared production settings validator also runs in the
worker, which is why the worker manifest includes the public URL/origin values
and session/token secrets even though it has no ingress.

## 12. Verify the complete stack

Check the active revisions, API health, migrations, and worker registration:

```bash
az containerapp revision list --name "$API_APP" --resource-group "$RG" \
  --query '[].{name:name,active:properties.active,health:properties.healthState}' -o table
az containerapp revision list --name "$PREFECT_APP" --resource-group "$RG" \
  --query '[].{name:name,active:properties.active,health:properties.healthState}' -o table
az containerapp revision list --name "$WORKER_APP" --resource-group "$RG" \
  --query '[].{name:name,active:properties.active,health:properties.healthState}' -o table

az containerapp logs show --name "$API_APP" --resource-group "$RG" --tail 100
curl --fail --show-error "$PUBLIC_URL/api/health/live"
curl --fail --show-error "$PUBLIC_URL/api/health/ready"
```

Expected readiness output is `{"status":"ready"}`. Confirm the API log shows
the Alembic migration completing, then log in, add a small source, start a sync,
and confirm the flow reaches Prefect and is consumed by the worker.

## 13. Operations and troubleshooting

### Rotation and updates

Key Vault references are materialized as Container Apps secrets. After rotating
a Key Vault secret, restart or create a revision for every consuming app. Rotate
the PostgreSQL credential and its Key Vault URL as one controlled operation.
Deploy new immutable image tags as separate revisions and review API migration
logs before switching traffic.

### Common failures

| Symptom | Correction |
| --- | --- |
| API fails production startup | Verify HTTPS URLs, non-empty origin lists, `SCIP_INSTALL_DEPS_MODE=never`, and all `SANDBOX_*` values. |
| Sync endpoint returns `502` | Set `PREFECT_API_URL` to the internal Prefect FQDN plus `/api`, not the WSL service alias. |
| Prefect cannot start | Verify `PREFECT_API_DATABASE_CONNECTION_URL` references the `prefect` database with `?ssl=require`. |
| Worker loses repositories or CPGs | Confirm environment storage name `worker-data` is mounted at `/data`. |
| Database timeout | Check VNet/private DNS integration, port `5432`, and TLS in both URLs. |
| Extension creation fails | The server version/region does not support the required parameter/extension combination; do not continue with an incomplete database. |
| OAuth redirect mismatch | The GitHub callback must exactly equal `https://<public-host>/api/auth/callback`. |

### Configuration reference

| Variable | API | Worker | Prefect | Azure value |
| --- | --- | --- | --- | --- |
| `DATABASE_URL` | yes | yes | no | Key Vault reference to `contextmine` asyncpg URL |
| `PREFECT_API_DATABASE_CONNECTION_URL` | no | no | yes | Key Vault reference to `prefect` asyncpg URL |
| `PREFECT_API_URL` | yes | yes | no | internal Prefect FQDN plus `/api` |
| `PREFECT_DUE_INTERVAL_SECONDS` | optional | yes | no | `300` is the validated conservative interval |
| `MODEL_CALLS_ENABLED` | yes | yes | no | `false` for FTS-only operation; otherwise `true` plus provider credentials |
| `SANDBOX_*` | yes | yes | no | required production sandbox configuration |
| `PUBLIC_BASE_URL` / `MCP_OAUTH_BASE_URL` | yes | no | no | exact external HTTPS API origin |

For the local WSL deployment, service startup order, and local diagnostics, see
[WSL_CONTAINERS_SETUP.md](WSL_CONTAINERS_SETUP.md). The two environments share
core values, but WSL localhost ports and container aliases must not be copied
into an Azure Container Apps deployment.