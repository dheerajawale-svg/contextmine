I checked the **current ContextMine repository, its Docker Compose setup, Helm chart, production values, and Azure's current PostgreSQL/Container Apps capabilities**. The important finding is: **PostgreSQL is required**, and for Azure I would **not put PostgreSQL inside the same Container App in production**. ContextMine is actually a multi-service application, so the clean Azure design is **Azure Container Apps + Azure Database for PostgreSQL Flexible Server**. ([GitHub][1])

## 1. What ContextMine actually requires

The current architecture is:

```text
                    Internet
                       │
                       ▼
              ┌─────────────────┐
              │ ContextMine API │
              │ FastAPI + React │
              │      :8000      │
              └────────┬────────┘
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
      ┌────────────┐       ┌─────────────┐
      │ PostgreSQL │       │   Prefect   │
      │ pgvector   │       │   Server    │
      │ Apache AGE │       │    :4200    │
      └────────────┘       └──────┬──────┘
                                  │
                                  ▼
                           ┌─────────────┐
                           │   Worker    │
                           │ Web crawler │
                           │ Git indexer │
                           └─────────────┘
```

The repository explicitly lists **PostgreSQL, Prefect, worker and API** as separate components. PostgreSQL isn't optional in the application architecture. ([GitHub][1])

More importantly, the PostgreSQL image they use is **`ghcr.io/mayflower/pg4ai`**, not ordinary PostgreSQL. It provides the PostgreSQL capabilities ContextMine expects, including **pgvector and Apache AGE**. ([GitHub][2])

---

# 2. Can Azure PostgreSQL replace ContextMine's PostgreSQL?

**Yes. This is actually what I recommend.**

Azure Database for PostgreSQL Flexible Server now supports both:

| Extension           | Azure support |
| ------------------- | ------------- |
| `vector` / pgvector | Yes           |
| Apache AGE          | Yes           |
| Normal PostgreSQL   | Yes           |

Microsoft documents pgvector support for Flexible Server and Apache AGE support as well. ([Microsoft Learn][3])

So you don't need to run:

```text
PostgreSQL container
```

yourself.

Instead:

```text
Azure Container Apps
       │
       │ private connection
       ▼
Azure PostgreSQL Flexible Server
       │
       ├── contextmine database
       └── prefect database
```

ContextMine's own Helm documentation explicitly supports an **external PostgreSQL**, requiring two databases (`contextmine` and `prefect`) and the `vector` and `age` extensions. 

---

# 3. Recommended Azure architecture

For your use case, I'd deploy this:

```text
Azure
│
├── Resource Group
│
├── Container Apps Environment
│   │
│   ├── contextmine-api
│   │       └── :8000
│   │
│   ├── contextmine-worker
│   │
│   └── contextmine-prefect
│           └── :4200
│
├── PostgreSQL Flexible Server
│   ├── contextmine DB
│   └── prefect DB
│
├── Azure Container Registry
│   ├── contextmine-api
│   └── contextmine-worker
│
├── Key Vault
│   └── secrets
│
└── Log Analytics
```

I'd use **Container Apps rather than App Service**.

Azure Container Apps supports multiple containers, but Microsoft recommends separate Container Apps for independent services rather than putting unrelated services together as sidecars. ([Microsoft Learn][4])

---

# 4. Why I don't recommend App Service

You *can* technically run containers through App Service, and persistent `/home` storage is available. ([Microsoft Learn][5])

But ContextMine isn't a simple single-container web application.

It needs:

* API
* Worker
* Prefect
* persistent worker data
* PostgreSQL
* potentially CodeCharta
* background processing

Therefore:

| Option                              | Recommendation                         |
| ----------------------------------- | -------------------------------------- |
| App Service single container        | ❌                                      |
| App Service multi-container         | ⚠️ Possible but awkward                |
| Container Apps single container     | ⚠️ Incomplete                          |
| Container Apps multiple apps        | **✅ Recommended**                      |
| AKS + Helm                          | ✅ Best for large production deployment |
| Container Apps + managed PostgreSQL | **✅ Best balance**                     |

The project itself says Docker Compose is intended for development and **Kubernetes/Helm is recommended for production**. ([GitHub][1])

Container Apps is a reasonable Azure middle ground without taking on AKS complexity.

---

# 5. Step-by-step Azure deployment

## Phase 1 — Create Azure resources

Create:

```text
Resource Group
Container Registry
Container Apps Environment
PostgreSQL Flexible Server
Key Vault
Log Analytics
```

For example:

```powershell
az group create `
  --name rg-contextmine `
  --location centralindia
```

Then:

```powershell
az acr create `
  --resource-group rg-contextmine `
  --name <uniqueRegistryName> `
  --sku Basic
```

You can use another Azure region if your organization has a preferred region.

---

# 6. Create PostgreSQL Flexible Server

Create:

```text
Azure Database for PostgreSQL Flexible Server
```

I'd initially use something modest such as:

```text
Compute: Burstable
vCPU:    2
RAM:     ~4 GB
Storage: 32-64 GB
```

You can scale it later.

For production, I'd put PostgreSQL behind a **private network/private access** rather than expose it publicly.

---

# 7. Create the databases

Connect using `psql`, Azure Cloud Shell or another PostgreSQL client.

Create:

```sql
CREATE DATABASE contextmine;
CREATE DATABASE prefect;
```

ContextMine explicitly requires both databases when using an external PostgreSQL server. 

---

# 8. Enable pgvector

In Azure PostgreSQL:

```text
PostgreSQL server
   ↓
Server parameters
   ↓
azure.extensions
```

Add:

```text
VECTOR
```

Then connect to the `contextmine` database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Microsoft's current documentation confirms this procedure. ([Microsoft Learn][3])

---

# 9. Enable Apache AGE

This is important because ContextMine's PostgreSQL architecture uses AGE.

In:

```text
PostgreSQL
 → Server parameters
```

enable:

```text
azure.extensions = AGE
```

and:

```text
shared_preload_libraries = AGE
```

Restart the PostgreSQL server.

Then connect to **contextmine**:

```sql
CREATE EXTENSION IF NOT EXISTS age CASCADE;
```

Azure's current documentation explicitly supports this configuration. ([Microsoft Learn][6])

I'd verify:

```sql
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('vector', 'age');
```

You should see both.

---

# 10. Create the Container Apps environment

For example:

```powershell
az containerapp env create `
  --name cae-contextmine `
  --resource-group rg-contextmine `
  --location centralindia
```

Then deploy the API, worker and Prefect separately.

---

# 11. Build ContextMine images

The repository provides separate images for:

```text
contextmine-api
contextmine-worker
```

and also an analyzer image. 

From the repository:

```powershell
git clone https://github.com/mayflower/contextmine.git
cd contextmine
```

Build:

```powershell
docker build `
  -t <registry>.azurecr.io/contextmine-api:latest `
  -f apps/api/Dockerfile .
```

and:

```powershell
docker build `
  -t <registry>.azurecr.io/contextmine-worker:latest `
  -f apps/worker/Dockerfile .
```

Then push:

```powershell
az acr login --name <registry>

docker push <registry>.azurecr.io/contextmine-api:latest
docker push <registry>.azurecr.io/contextmine-worker:latest
```

The project also publishes prebuilt images to GHCR, so building your own images isn't strictly necessary if your organization's policy allows pulling directly from GHCR. 

For an organization, I'd use **your own ACR**.

---

# 12. Configure ContextMine environment variables

The critical variables are:

```text
DATABASE_URL
GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET
SESSION_SECRET
TOKEN_ENCRYPTION_KEY
PUBLIC_BASE_URL
MCP_OAUTH_BASE_URL
MCP_ALLOWED_ORIGINS
```

The repository marks PostgreSQL and the security/authentication values as required. 

Your database URL will look like:

```text
postgresql+asyncpg://contextmine:<PASSWORD>@<SERVER>.postgres.database.azure.com:5432/contextmine?ssl=require
```

And Prefect needs:

```text
postgresql+asyncpg://contextmine:<PASSWORD>@<SERVER>.postgres.database.azure.com:5432/prefect?ssl=require
```

**Don't put these directly in the container image.**

Use Container Apps secrets or, preferably for an organization, Key Vault-backed secrets.

---

# 13. GitHub OAuth

ContextMine uses GitHub OAuth for authentication.

Create an OAuth application in GitHub.

For example:

```text
Homepage:
https://contextmine.yourcompany.com

Callback:
https://contextmine.yourcompany.com/api/auth/callback
```

The repository specifically documents the callback mechanism. ([GitHub][1])

Then configure:

```text
GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET
```

---

# 14. Generate the security keys

Generate two separate random values:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Run it twice.

Use them for:

```text
SESSION_SECRET
TOKEN_ENCRYPTION_KEY
```

The repository explicitly recommends generating secure random values for production. 

---

# 15. Deploy ContextMine API

The API needs:

```text
Port: 8000
Ingress: External
```

Conceptually:

```text
https://contextmine.company.com
        │
        ▼
contextmine-api:8000
```

The API serves:

```text
/api/*
/mcp/*
/*
```

so you don't need a separate frontend deployment. ([GitHub][1])

---

# 16. Deploy Prefect

ContextMine uses Prefect for background sync orchestration.

The Docker Compose configuration uses:

```text
prefecthq/prefect
```

with:

```text
prefect server start --host 0.0.0.0
```

and PostgreSQL as its backend. ([GitHub][2])

Create another Container App:

```text
contextmine-prefect
```

Port:

```text
4200
```

It does **not** need public ingress.

The worker should communicate internally with:

```text
http://contextmine-prefect:4200/api
```

rather than exposing Prefect to the Internet.

---

# 17. Deploy the worker

Create:

```text
contextmine-worker
```

This is particularly important because **the worker performs the actual source synchronization/crawling/indexing**.

It needs persistent storage.

The repository's production configuration gives the worker a persistent volume because it stores imported analyzer artifacts and crawler data. 

This is one reason Container Apps is preferable to a simplistic App Service deployment.

---

# 18. Persistent worker storage

You need to decide how important persistence of crawler/cache data is.

For Container Apps, you can use Azure Files-backed storage.

Architecture:

```text
contextmine-worker
       │
       ▼
Azure Files
       │
       └── /data
```

The ContextMine Compose configuration explicitly mounts:

```text
worker_data:/data
```

for repository/crawl cache persistence. ([GitHub][2])

For a first deployment, I'd allocate:

```text
50–100 GB Azure Files
```

depending on how many repositories/websites you intend to index.

---

# 19. Important: the Sandbox requirement

There is one **very important complication** I found in the current version.

ContextMine's production configuration contains:

```text
SANDBOX_API_URL
SANDBOX_API_KEY
SANDBOX_ANALYZER_SNAPSHOT
```

and its production Helm configuration points to a **Mayflower Agent Sandbox platform**. 

The project's production configuration says repository analysis is sandboxed, and the Helm documentation requires publishing the analyzer image as the configured sandbox snapshot. 

Therefore:

### If you only want

```text
Public documentation websites
+
internal documentation
+
MCP search
```

you should investigate whether you can run ContextMine with:

```text
MODEL_CALLS_ENABLED=false
```

and without GitHub/code-analysis functionality initially.

The project explicitly says that with model calls disabled, deterministic indexing and full-text retrieval still work, while model-dependent enrichment/research features are unavailable. 

### If you want GitHub/code intelligence

Then you need to solve the sandbox component as well.

That is a **separate deployment concern** from PostgreSQL.

---

# 20. Do you need OpenAI API?

No, not necessarily.

The current configuration allows:

```text
MODEL_CALLS_ENABLED=false
```

which gives you:

```text
Document extraction
        ↓
Full-text indexing
        ↓
MCP search
```

without external model calls. 

If you enable embeddings:

```text
MODEL_CALLS_ENABLED=true
```

you can configure an embedding provider such as OpenAI/Gemini.

That means there are two privacy models:

| Setup                                  |   Confidential documents leave Azure? |
| -------------------------------------- | ------------------------------------: |
| FTS only                               |                                **No** |
| Azure PostgreSQL + external embeddings | Embedding requests/chunks leave Azure |
| Azure PostgreSQL + Azure OpenAI        |             Controlled Azure boundary |
| Fully local embedding model            |                                **No** |

For organizational confidential material, I'd strongly consider **Azure OpenAI or locally hosted embeddings**, rather than sending chunks to a public API.

---

# 21. Connect Codex

Once the API is running:

```text
https://contextmine.company.com/mcp
```

Your Codex MCP configuration would conceptually be:

```toml
[mcp_servers.contextmine]
url = "https://contextmine.company.com/mcp"
```

ContextMine uses OAuth authentication for MCP clients. ([GitHub][1])

---

# 22. Add your first public website

After logging into ContextMine:

```text
Collection
    ↓
Add Source
    ↓
Web
    ↓
https://learn.microsoft.com/
    ↓
Sync
```

The crawler follows links within the same domain. ([GitHub][1])

You can therefore have:

```text
Collection: Engineering Knowledge

├── Internal
│   ├── Architecture
│   ├── API documentation
│   └── Engineering standards
│
├── Microsoft
│   └── learn.microsoft.com
│
├── Azure
│   └── Azure documentation
│
├── Kubernetes
│   └── kubernetes.io/docs
│
└── GitHub
    └── company/repository
```

Then Codex gets a single MCP endpoint.

---

# 23. What I'd actually deploy

For your stated requirement, I'd start **simpler than the full production Helm deployment**:

```text
                 Azure
                  │
       ┌──────────┴──────────┐
       │                     │
       ▼                     ▼
Container Apps         PostgreSQL Flexible
       │                     │
       ├─ API ───────────────┤
       │                     │
       ├─ Worker ────────────┤
       │                     │
       └─ Prefect ───────────┘
               │
               ▼
          Azure Files

               +
         Azure Key Vault

               +
             ACR
```

Then:

```text
Codex
  │
  ▼
ContextMine MCP
  │
  ├── Internal documents
  ├── Public web documentation
  └── GitHub repositories
```

## The biggest takeaway

**Don't put PostgreSQL inside the ContextMine Container App unless this is only a temporary/test installation.** ContextMine requires PostgreSQL and uses `pgvector + AGE`; Azure Flexible Server now supports both, so a managed PostgreSQL instance is a much better fit. 

For your particular goal, I'd deploy **API + Worker + Prefect as three Container Apps, PostgreSQL Flexible Server separately, Azure Files for worker persistence, ACR for images, and Key Vault for secrets**. This gives you a relatively inexpensive Azure deployment without taking on AKS.

If you want, I can next give you the **actual Azure CLI implementation end-to-end** — resource creation, PostgreSQL/AGE/pgvector setup, ACR, Container Apps YAML, secrets, volumes, GitHub OAuth, ContextMine environment variables, and Codex MCP configuration — so you can copy/paste the commands and deploy it.

[1]: https://github.com/mayflower/contextmine "GitHub - mayflower/contextmine: Documentation & Code Indexing with MCP - Open-source Context7 alternative · GitHub"
[2]: https://raw.githubusercontent.com/mayflower/contextmine/main/docker-compose.yml "raw.githubusercontent.com"
[3]: https://learn.microsoft.com/en-us/azure/postgresql/extensions/how-to-use-pgvector?utm_source=chatgpt.com "Vector Search in Azure Database for PostgreSQL Flexible Server - Azure Database for PostgreSQL | Microsoft Learn"
[4]: https://learn.microsoft.com/en-us/azure/container-apps/containers?utm_source=chatgpt.com "Containers in Azure Container Apps | Microsoft Learn"
[5]: https://learn.microsoft.com/en-us/azure/app-service/configure-custom-container?utm_source=chatgpt.com "Configure a Custom Container - Azure App Service | Microsoft Learn"
[6]: https://learn.microsoft.com/en-us/azure/postgresql/azure-ai/generative-ai-age-overview?utm_source=chatgpt.com "Apache AGE Extension - Azure Database for PostgreSQL | Microsoft Learn"