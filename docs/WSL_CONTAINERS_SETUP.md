# Running the Full Stack Locally with WSL Containers (`wslc.exe`)

Microsoft's **WSL container** feature replaces the Docker engine with `wslc.exe`, a Docker-familiar CLI built directly into WSL — no Docker Desktop required.

> **Repo location:** the repo now lives on the WSL filesystem at `\\wsl.localhost\Ubuntu\home\dheeraj\gitrepos\personal\contextmine` — inside WSL that same path is `/home/dheeraj/gitrepos/personal/contextmine` (`~/gitrepos/personal/contextmine`). It was previously at `C:\personal\contextmine`.

References:

- [WSL container](https://learn.microsoft.com/windows/wsl/wsl-container)
- [Get started with WSL container](https://learn.microsoft.com/windows/wsl/tutorials/wsl-containers)

`wslc` supports `run` (with `-d`, `-p` port publishing, `--name`, `--rm`, `-e`), `build` (Dockerfiles/Containerfiles), `exec`, `container list/stop/logs/inspect/prune`, `image list/prune`, and `stats`.

> **Key difference vs. Docker Compose:** `wslc` has **no compose equivalent** — each compose service becomes its own `wslc run` command. Service-to-service DNS requires a **user-defined network**: the default `bridge` network does *not* resolve container names (see section 3.0 and the networking note).

## 1. Prerequisites (one-time, PowerShell as admin)

```powershell
# WSL container requires WSL >= 2.9.3 (pre-release channel as of the docs)
wsl --update --pre-release
wsl --version          # confirm >= 2.9.3

# Verify wslc is available and working
wslc version
wslc run --rm hello-world
```

Also configure `.env` exactly as in the Docker guide (`cp .env.example .env`, GitHub OAuth keys, secrets, `MODEL_CALLS_ENABLED` choice) — nothing there changes.

## 2. Build the two app images

`wslc build` consumes the existing Dockerfiles unchanged. Note the build context is the **repo root** (per `docker-compose.yml`).

```bash
# From a WSL terminal — repo is on the WSL filesystem at ~/gitrepos/personal/contextmine
cd ~/gitrepos/personal/contextmine

# API image
wslc.exe build -t contextmine-api -f apps/api/Dockerfile .

# Worker image (the final stage in apps/worker/Dockerfile is the slim orchestration worker)
wslc.exe build -t contextmine-worker -f apps/worker/Dockerfile .

wslc.exe image list   # verify both, plus pulled infra images
```

> **Performance tip (from MS docs):** already satisfied — the repo is on the WSL filesystem (`\\wsl.localhost\Ubuntu\home\dheeraj\gitrepos\personal\contextmine`), not on a Windows drive like the old `C:\personal\contextmine` location, so builds and mounts run at native WSL speed.

## 3. Run the services (in dependency order)

> Run all `wslc` commands below from the same **WSL terminal** as the build step (section 2). `wslc` is a Windows binary — from inside WSL always call it as **`wslc.exe`** (WSL interop resolves it); from PowerShell plain `wslc` works. If `wslc.exe` is not found in WSL, enable `[interop]` `enabled=true` / `appendWindowsPath=true` in `/etc/wsl.conf`, then `wsl --shutdown` from Windows and reopen.

### 3.0 Create a user-defined network (required for container-name DNS)

The default `bridge` network does **not** register container names in DNS (verified: `gethostbyname` fails with `Temporary failure in name resolution`; `container inspect` shows `"Aliases": []`). Creating a user-defined network and attaching containers with `--network-alias` gives working container-name DNS:

```bash
wslc.exe network create contextmine-net
```

### 3.1 Postgres + pgvector + AGE

```bash
wslc.exe run -d --name contextmine-postgres \
  --network contextmine-net --network-alias contextmine-postgres \
  -p 5433:5432 \
  -e POSTGRES_USER=contextmine -e POSTGRES_PASSWORD=contextmine -e POSTGRES_DB=contextmine \
  -v "$PWD/.wslc/pgdata":/var/lib/postgresql \
  -v "$PWD/scripts/docker/init-db.sql":/docker-entrypoint-initdb.d/10-init-db.sql \
  ghcr.io/mayflower/pg4ai:sha-87808a9
```
> The pg4ai image is `linux/amd64`-only (compose sets `platform: linux/amd64`). `wslc` platform-emulation support is undocumented — on ARM machines verify with `wslc container logs contextmine-postgres` that it actually starts.

### 3.2 Prefect server (worker orchestration UI)

```bash
wslc.exe run -d --name contextmine-prefect \
  --network contextmine-net --network-alias contextmine-prefect \
  -p 4200:4200 \
  -e PREFECT_SERVER_API_HOST=0.0.0.0 \
  -e PREFECT_UI_API_URL=http://localhost:4200/api \
  -e PREFECT_API_DATABASE_CONNECTION_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/prefect \
  prefecthq/prefect:3.8.4-python3.14 prefect server start --host 0.0.0.0
```

### 3.3 API

```bash
# Minimal local dev run (DEBUG=true allows unauthenticated dev mode without GitHub OAuth credentials)
wslc.exe run -d --name contextmine-api \
  --network contextmine-net --network-alias contextmine-api \
  -p 8111:8000 \
  -e DEBUG=true \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/contextmine \
  -e SESSION_SECRET=dev-session-secret-change-in-production \
  -e TOKEN_ENCRYPTION_KEY=dev-encryption-key-change-in-production \
  contextmine-api
```

*(Pass every needed `.env` value as `-e` flags; `wslc` `--env-file` support is undocumented — check `wslc run --help`. Note: `DEBUG=true` bypasses the requirement for `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` for local dev. If testing the full GitHub OAuth flow, append `-e GITHUB_CLIENT_ID=... -e GITHUB_CLIENT_SECRET=...`).*

### 3.4 Worker

```bash
wslc.exe run -d --name contextmine-worker \
  --network contextmine-net --network-alias contextmine-worker \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/contextmine \
  -e PREFECT_API_URL=http://contextmine-prefect:4200/api \
  -v "$PWD/.wslc/worker-data":/data \
  contextmine-worker
```

### 3.5 Optional: CodeCharta and OTEL

```bash
wslc.exe run -d --name contextmine-codecharta --network contextmine-net -p 9001:80 codecharta/codecharta-visualization:1.143.0
# OTEL collector: otel/opentelemetry-collector-contrib:0.159.0 with the config from scripts/docker/otel/
```

## ⚠️ Networking note — container-name DNS

Container-name DNS **does not work on the default `bridge` network** in `wslc` (verified: `gethostbyname` → `Temporary failure in name resolution`; `container inspect` shows `"Aliases": []`). It **does work on a user-defined network** — `wslc` supports `network create` / `network connect` with `--network-alias` (verified working: name resolution and TCP connectivity to `contextmine-postgres:5432`).

**If your containers were started without the network flags** (sections 3.1–3.5 now include them), fix in place without recreating containers:

```bash
wslc.exe network create contextmine-net
wslc.exe network connect --network-alias contextmine-postgres contextmine-net contextmine-postgres
wslc.exe network connect --network-alias contextmine-prefect contextmine-net contextmine-prefect
wslc.exe network connect --network-alias contextmine-api contextmine-net contextmine-api
wslc.exe network connect --network-alias contextmine-worker contextmine-net contextmine-worker
# codecharta/otel if running:
wslc.exe network connect contextmine-net contextmine-codecharta
wslc.exe network connect contextmine-net contextmine-otel

# Verify
wslc.exe exec contextmine-api python -c "import socket; socket.gethostbyname('contextmine-postgres')"
```

- **If a container is already on `contextmine-net`** (started with `--network contextmine-net` per sections 3.1–3.5) → `network connect` for it is unnecessary; it can be skipped.
- **If host networking is preferred instead** → run all with `--network host` and use `localhost:5433` / `localhost:4200` everywhere.
- **If neither works** → use **Path B** below, which avoids container-to-container networking entirely.

> Note: containers keep their default-bridge connection after being attached to `contextmine-net`; published ports and existing connections are unaffected.

## 4. Migrations, verify, first sync

```bash
# Migrations (equivalent of the compose exec command)
wslc.exe exec contextmine-api sh -c "cd /app/packages/core && alembic upgrade head"

# Verify
wslc.exe container list                       # all containers Up
wslc.exe container logs contextmine-api       # startup errors
wslc.exe stats                                # resource usage
curl http://localhost:8111/api/health/ready
```

Then: admin UI at `http://localhost:8111` → GitHub OAuth login → create Collection → add Source → Sync, and watch the job at `http://localhost:4200` (Prefect UI). Point MCP clients at `http://localhost:8111/mcp`.

> Remember the OAuth callback in your GitHub App must match the API port you published (`8111`, unlike the native-dev default `8000`).

## 5. Quick restart after machine reboot

When Windows or WSL restarts, containers created without `--rm` persist in a stopped state. You do not need to rebuild images or recreate the network.

### Step 1: Start existing containers (in order)
> **Important:** `wslc.exe container start` takes only **one container ID/name at a time** (unlike `container stop`).

```bash
# Start in dependency order
wslc.exe container start contextmine-postgres
wslc.exe container start contextmine-prefect
wslc.exe container start contextmine-api
wslc.exe container start contextmine-worker

# Or as a one-liner loop:
for c in contextmine-postgres contextmine-prefect contextmine-api contextmine-worker; do wslc.exe container start "$c"; done
```

*(Note: `contextmine-api` automatically executes database migrations via Alembic on startup inside its entrypoint script).*

### (Alternative) If containers were deleted / recreating from scratch
If you ran `wslc.exe container prune` or need to recreate containers fresh:

```bash
# 1. Network (if not already present)
wslc.exe network create contextmine-net 2>/dev/null || true

# 2. Postgres
wslc.exe run -d --name contextmine-postgres \
  --network contextmine-net --network-alias contextmine-postgres \
  -p 5433:5432 \
  -e POSTGRES_USER=contextmine -e POSTGRES_PASSWORD=contextmine -e POSTGRES_DB=contextmine \
  -v "$PWD/.wslc/pgdata":/var/lib/postgresql \
  -v "$PWD/scripts/docker/init-db.sql":/docker-entrypoint-initdb.d/10-init-db.sql \
  ghcr.io/mayflower/pg4ai:sha-87808a9

# 3. Prefect
wslc.exe run -d --name contextmine-prefect \
  --network contextmine-net --network-alias contextmine-prefect \
  -p 4200:4200 \
  -e PREFECT_SERVER_API_HOST=0.0.0.0 \
  -e PREFECT_UI_API_URL=http://localhost:4200/api \
  -e PREFECT_API_DATABASE_CONNECTION_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/prefect \
  prefecthq/prefect:3.8.4-python3.14 prefect server start --host 0.0.0.0

# 4. API (DEBUG=true allows unauthenticated dev mode without GitHub OAuth)
wslc.exe run -d --name contextmine-api \
  --network contextmine-net --network-alias contextmine-api \
  -p 8111:8000 \
  -e DEBUG=true \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/contextmine \
  -e SESSION_SECRET=dev-session-secret-change-in-production \
  -e TOKEN_ENCRYPTION_KEY=dev-encryption-key-change-in-production \
  contextmine-api

# 5. Worker
wslc.exe run -d --name contextmine-worker \
  --network contextmine-net --network-alias contextmine-worker \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/contextmine \
  -e PREFECT_API_URL=http://contextmine-prefect:4200/api \
  -v "$PWD/.wslc/worker-data":/data \
  contextmine-worker
```

### Step 2: Verification commands

```bash
# 1. Check all 4 containers are running (Status: Up)
wslc.exe container list

# 2. Check API readiness & liveness (returns HTTP 200 {"status":"ready"})
curl -i http://localhost:8111/api/health/ready
curl -i http://localhost:8111/api/health/live

# 3. Verify container-to-container DNS from API to Postgres
wslc.exe exec contextmine-api python -c "import socket; print('Postgres IP:', socket.gethostbyname('contextmine-postgres'))"

# 4. View container logs if needed
wslc.exe container logs contextmine-api
wslc.exe container logs -f contextmine-api   # follow live
```

### Application URLs

| Service | URL | Notes |
|---|---|---|
| **Web UI (Frontend)** | [http://localhost:8111](http://localhost:8111) | Accessible directly in your Windows browser |
| **API Interactive Docs (Swagger)** | [http://localhost:8111/docs](http://localhost:8111/docs) | OpenAPI specification and interactive test UI |
| **API Health Endpoint** | [http://localhost:8111/api/health/ready](http://localhost:8111/api/health/ready) | Returns `{"status":"ready"}` |
| **MCP Server Endpoint** | `http://localhost:8111/mcp` | FastMCP endpoint for AI agent integrations |
| **Prefect Orchestration UI** | [http://localhost:4200](http://localhost:4200) | Dashboard for workflow runs and background sync jobs |
| **CodeCharta (optional)** | [http://localhost:9001](http://localhost:9001) | Code visualization UI (if container started) |

## Path B (recommended fallback): WSL-native hybrid

Per the MS docs, WSL *is* a full Linux environment — so the most robust WSL setup runs only **stateful infra in `wslc` containers** (Postgres + Prefect) and everything else natively inside your WSL distro, eliminating the undocumented container-networking concern:

```bash
# Inside WSL (Ubuntu) — repo root: ~/gitrepos/personal/contextmine
# Infra containers already running via wslc (sections 3.1–3.2):
cd ~/gitrepos/personal/contextmine

uv sync --all-packages

cd packages/core && DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@localhost:5433/contextmine \
  uv run alembic upgrade head && cd ../..

cd apps/web && npm install && npm run build && cd ../..

# Terminal 1: API
STATIC_DIR=apps/web/dist uv run uvicorn apps.api.app.main:app --reload --port 8000

# Terminal 2: worker
uv run python -m contextmine_worker.main
```

From WSL, containers on published ports are reachable at `localhost:5433` / `localhost:4200` (WSL2 localhost forwarding), which matches the documented, guaranteed behavior.

## 6. Teardown & cleanup

```bash
wslc.exe container stop contextmine-worker contextmine-api contextmine-prefect contextmine-postgres
wslc.exe container prune        # remove stopped containers
wslc.exe network remove contextmine-net   # remove the user-defined network
wslc.exe image prune            # reclaim image disk space
wslc.exe container inspect contextmine-api   # deep debug when needed
```

## Caveats vs. Docker Compose

| Compose feature | wslc status |
|---|---|
| `docker compose up` (multi-service, healthcheck-gated startup) | ❌ No compose — start manually in dependency order; gate on `wslc container list`/`logs` |
| Service-name DNS (`postgres`, `prefect-server`) | ✅ Works on a **user-defined network** (`wslc network create` + `--network` / `--network-alias` per container) — default `bridge` does **not** resolve names, see networking note |
| `platform: linux/amd64` emulation | ⚠️ Undocumented — pg4ai image may fail on ARM |
| `env_file: .env` | ⚠️ Pass `-e` flags (check for `--env-file` in `wslc run --help`) |
| Named volumes (`postgres_data`, `worker_data`) | ⚠️ Use bind mounts (`-v`) to a local dir |
| Hot-reload source mounts | ⚠️ Bind-mount support exists in the API; verify CLI flags via `wslc run --help` |

The feature itself is **pre-release** (WSL ≥ 2.9.3), so treat flag-level details as subject to change — `wslc --help` is the authoritative reference on your machine.
