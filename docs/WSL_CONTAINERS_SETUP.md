# Running the Full Stack Locally with WSL Containers (`wslc.exe`)

Microsoft's **WSL container** feature replaces the Docker engine with `wslc.exe`, a Docker-familiar CLI built directly into WSL — no Docker Desktop required.

> **Repo location:** the repo now lives on the WSL filesystem at `\\wsl.localhost\Ubuntu\home\dheeraj\gitrepos\personal\contextmine` — inside WSL that same path is `/home/dheeraj/gitrepos/personal/contextmine` (`~/gitrepos/personal/contextmine`). It was previously at `C:\personal\contextmine`.

References:

- [WSL container](https://learn.microsoft.com/windows/wsl/wsl-container)
- [Get started with WSL container](https://learn.microsoft.com/windows/wsl/tutorials/wsl-containers)

`wslc` supports `run` (with `-d`, `-p` port publishing, `--name`, `--rm`, `-e`), `build` (Dockerfiles/Containerfiles), `exec`, `container list/stop/logs/inspect/prune`, `image list/prune`, and `stats`.

> **Key difference vs. Docker Compose:** `wslc` has **no compose equivalent** — each compose service becomes its own `wslc run` command, and service-to-service DNS names (`postgres`, `prefect-server`) are not documented, so this guide uses published ports and flags the one networking check you need to do.

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
wslc build -t contextmine-api -f apps/api/Dockerfile .

# Worker image (the final stage in apps/worker/Dockerfile is the slim orchestration worker)
wslc build -t contextmine-worker -f apps/worker/Dockerfile .

wslc image list   # verify both, plus pulled infra images
```

> **Performance tip (from MS docs):** already satisfied — the repo is on the WSL filesystem (`\\wsl.localhost\Ubuntu\home\dheeraj\gitrepos\personal\contextmine`), not on a Windows drive like the old `C:\personal\contextmine` location, so builds and mounts run at native WSL speed.

## 3. Run the services (in dependency order)

> Run all `wslc` commands below from the same **WSL terminal** as the build step (section 2). Commands without repo-relative paths (build, exec, list, logs, stats, stop, prune) also work from PowerShell via `wslc.exe`.

### 3.1 Postgres + pgvector + AGE

```bash
wslc run -d --name contextmine-postgres \
  -p 5433:5432 \
  -e POSTGRES_USER=contextmine -e POSTGRES_PASSWORD=contextmine -e POSTGRES_DB=contextmine \
  -v "$PWD/.wslc/pgdata":/var/lib/postgresql \
  -v "$PWD/scripts/docker/init-db.sql":/docker-entrypoint-initdb.d/10-init-db.sql \
  ghcr.io/mayflower/pg4ai:sha-87808a9
```

> The pg4ai image is `linux/amd64`-only (compose sets `platform: linux/amd64`). `wslc` platform-emulation support is undocumented — on ARM machines verify with `wslc container logs contextmine-postgres` that it actually starts.

### 3.2 Prefect server (worker orchestration UI)

```bash
wslc run -d --name contextmine-prefect \
  -p 4200:4200 \
  -e PREFECT_SERVER_API_HOST=0.0.0.0 \
  -e PREFECT_UI_API_URL=http://localhost:4200/api \
  -e PREFECT_API_DATABASE_CONNECTION_URL=postgresql+asyncpg://contextmine:contextmine@<PG_ADDRESS>:5432/prefect \
  prefecthq/prefect:3.8.4-python3.14 prefect server start --host 0.0.0.0
```

### 3.3 API

```bash
wslc run -d --name contextmine-api \
  -p 8111:8000 \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@<PG_ADDRESS>:5432/contextmine \
  -e GITHUB_CLIENT_ID=... -e GITHUB_CLIENT_SECRET=... \
  -e SESSION_SECRET=... -e TOKEN_ENCRYPTION_KEY=... \
  contextmine-api
```

*(Pass every needed `.env` value as `-e` flags; `wslc` `--env-file` support is undocumented — check `wslc run --help`. For dev hot-reload you can additionally bind-mount the source read-only if `-v` supports it, mirroring the compose mounts.)*

### 3.4 Worker

```bash
wslc run -d --name contextmine-worker \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@<PG_ADDRESS>:5432/contextmine \
  -e PREFECT_API_URL=http://<PREFECT_ADDRESS>:4200/api \
  -v "$PWD/.wslc/worker-data":/data \
  contextmine-worker
```

### 3.5 Optional: CodeCharta and OTEL

```bash
wslc run -d --name contextmine-codecharta -p 9001:80 codecharta/codecharta-visualization:1.143.0
# OTEL collector: otel/opentelemetry-collector-contrib:0.159.0 with the config from scripts/docker/otel/
```

## ⚠️ Networking check — `<PG_ADDRESS>` / `<PREFECT_ADDRESS>`

Compose relies on embedded DNS (`postgres`, `prefect-server`). That isn't a documented `wslc` feature, so first test what an app container can reach:

```bash
wslc exec contextmine-api python -c "import socket; socket.gethostbyname('contextmine-postgres')"
```

- **If name resolution works** → use `contextmine-postgres:5432` / `contextmine-prefect:4200`.
- **If host networking is supported** (check `wslc run --help` for `--network`) → run all with host networking and use `localhost:5433` / `localhost:4200` everywhere.
- **If neither works** → use **Path B** below, which avoids container-to-container networking entirely.

## 4. Migrations, verify, first sync

```bash
# Migrations (equivalent of the compose exec command)
wslc exec contextmine-api sh -c "cd /app/packages/core && alembic upgrade head"

# Verify
wslc container list                       # all containers Up
wslc container logs contextmine-api       # startup errors
wslc stats                                # resource usage
curl http://localhost:8111/api/health/ready
```

Then: admin UI at `http://localhost:8111` → GitHub OAuth login → create Collection → add Source → Sync, and watch the job at `http://localhost:4200` (Prefect UI). Point MCP clients at `http://localhost:8111/mcp`.

> Remember the OAuth callback in your GitHub App must match the API port you published (`8111`, unlike the native-dev default `8000`).

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

## 5. Teardown & cleanup

```bash
wslc container stop contextmine-worker contextmine-api contextmine-prefect contextmine-postgres
wslc container prune        # remove stopped containers
wslc image prune            # reclaim image disk space
wslc container inspect contextmine-api   # deep debug when needed
```

## Caveats vs. Docker Compose

| Compose feature | wslc status |
|---|---|
| `docker compose up` (multi-service, healthcheck-gated startup) | ❌ No compose — start manually in dependency order; gate on `wslc container list`/`logs` |
| Service-name DNS (`postgres`, `prefect-server`) | ⚠️ Undocumented — verify per networking check; Path B avoids it |
| `platform: linux/amd64` emulation | ⚠️ Undocumented — pg4ai image may fail on ARM |
| `env_file: .env` | ⚠️ Pass `-e` flags (check for `--env-file` in `wslc run --help`) |
| Named volumes (`postgres_data`, `worker_data`) | ⚠️ Use bind mounts (`-v`) to a local dir |
| Hot-reload source mounts | ⚠️ Bind-mount support exists in the API; verify CLI flags via `wslc run --help` |

The feature itself is **pre-release** (WSL ≥ 2.9.3), so treat flag-level details as subject to change — `wslc --help` is the authoritative reference on your machine.
