# Troubleshooting Log — ContextMine on WSL Containers

Findings and resolutions from the 2026-09-08 debugging session. Covers the context-generation
failure, model-free operation, Prefect connectivity, and shallow web-crawl indexing.

## 1. "An error occurred while generating context"

**Symptom:** Every context-generation request (`POST /api/context/stream`) failed with the
generic SSE error `{"error": "An error occurred while generating context"}`.

**Root cause (two factors):**

1. The `contextmine-api` container ran a **stale image** (built before the working-tree fix in
   `packages/core/contextmine_core/context.py`). The old `_get_query_embedding` fell back to
   `FakeEmbedder` when no embedding API key was configured.
2. **`FakeEmbedder` NaN bug** (`packages/core/contextmine_core/embeddings.py`): it reinterprets
   raw SHA-256 bytes as float32 via `struct.unpack("f", ...)`. ~0.4% of 4-byte patterns decode as
   NaN/inf, so virtually every 1536-dim vector contained NaNs (e.g. query `"a"` → 9 NaNs). The
   `magnitude > 0` guard is `False` for NaN, so they were never normalized away. pgvector rejects
   such input: `asyncpg.exceptions.DataError: NaN not allowed in vector`.

**Fix:**

- `embeddings.py`: non-finite unpacked floats are coerced to `0.0` before normalization
  (`math.isfinite` guard). Benefits every fallback path: API search route, MCP raw-chunks,
  worker sync embedding fallback.
- Regression test: `test_embeddings_are_finite_for_pgvector` in
  `packages/core/tests/test_misc_coverage.py`.
- Rebuilt the `contextmine-api` and `contextmine-worker` images so containers run the current
  code (the working tree's `context.py` now degrades to FTS-only search when no embedding
  provider is available, instead of using `FakeEmbedder` for queries).

## 2. AI model keys — NOT required

The app works fully without OpenAI/Gemini/Anthropic keys:

- **Retrieval:** Postgres full-text search only (vector search skipped).
- **Synthesis:** `FakeLLM` fallback, or skipped entirely with `MODEL_CALLS_ENABLED=false`
  (retrieval-only markdown, explicitly labeled in output).
- **Sync/indexing:** model-free mode skips embedding generation.

Both API and worker containers now run with `-e MODEL_CALLS_ENABLED=false`. Keys remain optional
upgrades for semantic (vector) search and real LLM answers.

## 3. `sync-now` returned 502 "Prefect scheduling failed"

**Root cause:** the API container was missing `PREFECT_API_URL`. The settings default is
`http://prefect-server:4200/api` (the docker-compose service name), which does not resolve on
the `contextmine-net` wslc network where the Prefect container alias is `contextmine-prefect`.
(Scheduled syncs still worked because the *worker* container had the correct URL.)

**Fix:** API container recreated with `-e PREFECT_API_URL=http://contextmine-prefect:4200/api`;
both API run commands in `docs/WSL_CONTAINERS_SETUP.md` (§3.3 and §5) now include the flag.

## 4. Shallow web indexing (only 2 pages from a full docs site)

**Symptom:** The Ravnur docs source (`docs.ravnur.com` Zendesk help center) indexed only 2
documents / 2 chunks.

**Root cause:** `validate_web_url` derives `base_url` (the crawl scope prefix) as the parent
path of the start URL. The source pointed at `.../hc/en-us/categories/<id>-RMS-API-reference`,
so the scope was `.../hc/en-us/categories/`. Zendesk keeps actual content under
`/hc/en-us/articles/` and `/hc/en-us/sections/` — outside the prefix, so the crawler never
reached it.

**Fix (source config in DB):**

```json
{
  "start_url": "https://docs.ravnur.com/hc/en-us/categories/22394674649490-RMS-API-reference",
  "base_url": "https://docs.ravnur.com/hc/en-us/",
  "max_pages": 500,
  "exclude_patterns": ["/related/click", "/search?"]
}
```

**New `exclude_patterns` option** (web sources): case-insensitive URL substrings skipped by the
crawler — in the Python crawler's link enqueue/fetch and in the spider_md output filter
(`apps/worker/contextmine_worker/web_sync.py`, wired in `flows.py::sync_web_source`). Without
it, Zendesk `/related/click?...` redirect stubs and `/search?...` pages polluted the index
(281 of 481 docs = 54% noise after the first deep crawl). The sync's diff logic hard-deletes
documents not seen in a run, so re-syncing with the filter removed the noise automatically.

## 5. Verified end state

| Metric | Initial | After scope fix | After noise filter |
|---|---|---|---|
| Documents | 2 | 481 | 186 (100% real content) |
| Chunks | 2 | 7522 | 1725 |
| `/related/click` + search pages | — | 281 (54%) | 0 |

- Context generation streams successfully with article-level sources; no AI keys configured.
- Full test suite: 4655 passed (core+api) / 812 passed (worker); ruff and ty clean
  (2 pre-existing `ty` warnings in `apps/worker/tests/test_flows_unit.py`, unrelated).
- Known unrelated issue: `packages/core/tests/test_native_typescript_lsp.py` fails collection
  locally — optional `multilspy` dependency not installed in the WSL dev environment.

## Quick reference — container run flags (wslc)

API:

```bash
wslc.exe run -d --name contextmine-api --network contextmine-net --network-alias contextmine-api \
  -p 8111:8000 -e DEBUG=true -e MODEL_CALLS_ENABLED=false \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/contextmine \
  -e PREFECT_API_URL=http://contextmine-prefect:4200/api \
  -e SESSION_SECRET=dev-session-secret-change-in-production \
  -e TOKEN_ENCRYPTION_KEY=dev-encryption-key-change-in-production contextmine-api
```

Worker:

```bash
wslc.exe run -d --name contextmine-worker --network contextmine-net --network-alias contextmine-worker \
  -e MODEL_CALLS_ENABLED=false \
  -e DATABASE_URL=postgresql+asyncpg://contextmine:contextmine@contextmine-postgres:5432/contextmine \
  -e PREFECT_API_URL=http://contextmine-prefect:4200/api \
  -v "$PWD/.wslc/worker-data":/data contextmine-worker
```

Manual sync trigger (dev login first to get the session cookie):

```bash
curl -c /tmp/cm-cookies.txt http://localhost:8111/api/auth/login
curl -b /tmp/cm-cookies.txt -X POST http://localhost:8111/api/sources/<source-id>/sync-now
```
