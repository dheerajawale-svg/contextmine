# Session Context — 2026-09-10 — Prefect scheduled-run noise & `PREFECT_DUE_INTERVAL_SECONDS` durable fix

Operational knowledge captured from a debugging/config session on the local WSL container stack.

## 1. User Goals & Intent

- Understand why the Prefect UI always shows many `Scheduled` runs with auto-generated names
  (e.g. `encouraging-hare`, `wealthy-panther`) that were never started manually.
- Confirm the already-indexed docs are not being needlessly re-indexed.
- Clean up the noise and apply a **durable** fix (survives container restarts/recreation).
- Understand what happens when more documents/sources are added.

## 2. Key Technical Context — how sync scheduling works

- Two Prefect deployments exist, both (re)applied by `configure_deployments()` in
  `apps/worker/contextmine_worker/main.py` **at every worker startup**:
  - `sync_due_sources/default` — dispatcher with an **interval schedule**
    (`settings.prefect_due_interval_seconds`, code default **60s**, `ge=10`).
    Each run does a cheap DB check (`SELECT sources WHERE enabled AND next_run_at <= now`)
    and exits in <1s when nothing is due.
  - `sync_single_source/default` — no schedule; triggered on demand (API `sync-now` or by the dispatcher).
- The funny run names are **Prefect's auto-generated flow-run names**. The scheduler also
  **pre-creates ~1h of future runs** — a wall of `Scheduled` runs is the normal steady state,
  not an error.
- Per-source re-sync cadence is `sources.schedule_interval_minutes` (API create default **1440 = 24h**;
  the Ravnur web source is set to 60). The dispatcher only *picks up* due sources; it does not itself re-index.
- `claim_due_source_runs()` (in `apps/worker/contextmine_worker/flows.py`) claims due sources with
  `FOR UPDATE SKIP LOCKED`, bumps `next_run_at`, skips sources with an active SCHEDULED/RUNNING sync,
  then launches `sync_single_source` with an idempotency key.

## 3. Environment & Configuration Details

- Stack per `docs/WSL_CONTAINERS_SETUP.md`: `contextmine-postgres` (5433→5432), `contextmine-prefect` (4200),
  `contextmine-api` (8111→8000), `contextmine-worker`; all on user-defined network `contextmine-net`.
- **`contextmine-worker` was recreated** on 2026-09-10 with env:
  `DATABASE_URL`, `PREFECT_API_URL=http://contextmine-prefect:4200/api`,
  **`PREFECT_DUE_INTERVAL_SECONDS=300`** (new), **`MODEL_CALLS_ENABLED=false`** (was already set on the
  container but missing from the doc — drift fixed), volume `.wslc/worker-data:/data`.
- Setup doc §3.4 now includes both env vars, so recreating from the doc keeps them.
- Verified live after change: deployment interval `300.0s, active=true`; scheduler repopulated
  **13 future runs spaced exactly +300s** (steady state ≈ 12–13 scheduled runs, down from ~60).
- `MODEL_CALLS_ENABLED=false` ⇒ embeddings skipped (`skip_reason=model_calls_disabled`), KG build
  deterministic (no LLM business rules / semantic entities / community summaries), retrieval FTS-only.
- DB state at session time: 1 enabled web source (Ravnur RMS API docs), 13 sync_runs total
  (6 success / 6 failed / 1 scheduled).

## 4. Discussion Highlights — what happened & actions taken

1. **Diagnosis:** 237 `SCHEDULED` runs existed; the oldest were past-due from 2026-09-08 (stack downtime).
   The worker was draining the backlog, so dispatcher runs were completing every ~8–10s instead of 60s.
   All runs were `sync_due_sources` no-op ticks — the docs were **not** being re-indexed every minute.
2. **Cleanup:** deleted **156 stale past-due** scheduled runs via the Prefect REST API
   (`POST /api/flow_runs/filter` + `DELETE /api/flow_runs/{id}`); kept the legitimately future ones.
3. **Durable fix:** an API-only schedule change would be reverted on worker restart (see §2), so the env var
   was set on the container itself + documented. Worker recreated (stop → rm → run) with no active syncs
   in flight (only harmless PENDING dispatcher no-ops).
4. **Gotcha:** multi-line `wslc.exe run` with `\` continuations failed in the agent shell
   ("invalid reference format", then each continuation ran as its own command). The stop/rm had already
   succeeded. **Recreate commands must be issued as a single line.**
5. **Adding more documents/sources (Q&A outcome):**
   - New source via `POST /collections/{id}/sources` sets `next_run_at = now()` → picked up automatically
     within ≤5 min (300s tick); `POST /sources/{id}/sync-now` fires immediately and is idempotent
     (`already_scheduled` if in flight).
   - Web sync is incremental per page: new URL → new doc/chunks/symbols; changed SHA-256 → re-chunk
     (chunk-hash diff); unchanged → only `last_seen_at` bumped; disappeared → document hard-deleted.
   - Watch out: `max_pages` default **100** per sync (raise per-source for big docs sites);
     crawl is rate-limited (500ms delay, `web-crawl` concurrency limit 3).

## 5. Issues, Assumptions & Open Questions

- 6 historical `failed` sync_runs exist — not investigated this session; worth checking
  (`SELECT error FROM sync_runs WHERE status='failed'`) if they recur.
- After future downtime, a small past-due backlog (≤ hours × 12 runs) still accumulates, but drains
  quickly and harmlessly at the 300s cadence.
- `prefect_due_interval_seconds` has `ge=10`; 300 chosen as noise/promptness trade-off.
  Worst-case pickup delay for a due source is now 5 min.

## 6. References & Contextual Notes

- `docs/WSL_CONTAINERS_SETUP.md` — §3.4 worker run command (updated this session).
- `apps/worker/contextmine_worker/main.py` — `configure_deployments()` applies the interval at startup.
- `packages/core/contextmine_core/settings.py` — `prefect_due_interval_seconds` (default 60),
  `prefect_sync_deployment`, `prefect_work_pool_name`.
- `apps/worker/contextmine_worker/flows.py` — `sync_due_sources`, `claim_due_source_runs`,
  `sync_web_source`, `_process_web_page`, `embed_document` (model-free skip).
- Prefect API: `http://localhost:4200/api`; useful filters:
  `POST /flow_runs/filter` with `{"flow_runs": {"state": {"type": {"any_": ["SCHEDULED"]}}}}`,
  `POST /deployments/filter`, `POST /flow_runs/count`.
