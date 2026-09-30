# PyPI Search API Server

A fast, read-only HTTP search and metadata API for Python packages, backed by SQLite with normalized B-tree prefix searching and FTS5 trigram substring matching.

## Features

- **Ranked Search**: Deduplicated exact name matches first, then indexed B-tree prefix matches, then FTS5 trigram substring matches. Ties broken alphabetically.
- **Short-Query Prefix Behavior**: One- and two-character queries use B-tree range prefix matching. Substring matching via FTS5 trigram starts at three characters.
- **Literal Query Semantics**: Queries are matched literally with case and separator normalization. Typo correction and fzf-style fuzzy matching are intentionally out of scope.
- **PEP 503 Normalization**: Fully supports PyPI normalization (lowercase, collapsing runs of `-`, `_`, and `.` to `-`).
- **Read-Only SQLite Engine**: Request-scoped, read-only connections with startup schema and FTS readiness validation. Pinned to SQLite 3.53.1+ runtime with FTS5 trigram support.
- **Measured Latencies**: Warm sequential queries achieve p95 ~5.5 ms and concurrent 5x queries achieve p95 ~15.7 ms on the complete 1,035,000+ package dataset (~1.11 GiB).

## API Endpoints

### 1. `GET /search`
Search packages by query string.

- **Query Parameters**:
  - `q` (string, required, max 100 characters): Package search query. Empty/whitespace queries return `[]`.
  - `limit` (integer, optional, default 50, range 1–100): Maximum results to return.
- **Matching & Ranking**:
  - 1-2 characters: exact matches followed by indexed prefix matches.
  - 3+ characters: exact matches, followed by prefix matches, followed by FTS5 trigram substring matches.
  - Deterministic alphabetical tie-breaking across matches in the same rank tier.
- **Response**: Array of `SearchResult` objects:
  ```json
  [
    {
      "name": "requests",
      "summary": "Python HTTP for Humans.",
      "version": "2.31.0",
      "upload_time": "2023-05-22T00:00:00Z"
    }
  ]
  ```

### 2. `GET /package/{name}`
Lookup complete package details.

- Matches exact name first, falling back to PEP 503 normalized name.
- Returns 404 if not found.
- Returns full package metadata (author, summary, license, requirements, classifiers, URLs).

### 3. `GET /health`
Verifies SQLite database accessibility, required tables, and search index readiness via read-only MATCH probe.
Returns `{"status": "ok", "sqlite_version": "3.53.1"}` (200) or 503 if unavailable.

---

## Performance Targets and Benchmark Results

Measured against the complete production snapshot:
- **Corpus**: 1,035,371 packages, 1.11 GiB SQLite database
- **Runtime**: Python 3.13.15, SQLite 3.53.1 with FTS5 trigram tokenizer
- **Machine**: Apple M-series / 2-vCPU equivalent container

| Metric | Target | Measured Result |
|---|---|---|
| Warm Sequential p50 | — | ~1.8 ms |
| Warm Sequential p95 | < 200 ms | ~5.5 ms |
| Warm Sequential p99 | — | ~6.5 ms |
| Concurrent (5x) p50 | — | ~10.4 ms |
| Concurrent (5x) p95 | < 500 ms | ~15.7 ms |
| Concurrent (5x) p99 | — | ~28.3 ms |
| Cold (First Pass) p95 | — | ~75.7 ms |

---

## Local Development

### 1. Requirements
- Python 3.13.15 (pinned in `mise.toml`)
- `uv`

### 2. Prepare Fixture Database
Generate a local test database with sample packages and indexes:
```bash
just fixture_db
# or from project root:
scripts/build_search_index.py --create-fixture server/pypi_data.sqlite
```

### 3. Run Dev Server
```bash
cd server
uv run uvicorn main:app --reload --port 8000
# or via Justfile:
just dev_server
```

### 4. Run Tests
```bash
just test
# or inside server/:
uv run pytest -v
```

### 5. Run Performance Benchmark
```bash
just benchmark
# or inside server/:
uv run python benchmark_search.py --iterations 30 --concurrency 5
```

---

## Docker & Container Registry (GHCR)

Published to GitHub Container Registry: `ghcr.io/iloveitaly/pypi-api`.

### One-Liner (`docker run`)
```bash
docker run -d -p 8000:8000 --restart unless-stopped ghcr.io/iloveitaly/pypi-api:latest
```

### Docker Compose

To host locally with Docker Compose:

```yaml
services:
  api:
    image: ghcr.io/iloveitaly/pypi-api:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      - PORT=8000
    healthcheck:
      test: ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 15s
```

Start the service:

```bash
docker compose up -d
curl http://localhost:8000/health
curl "http://localhost:8000/search?q=requests"
```

### Run on Custom Port
```bash
docker run -d -e PORT=8080 -p 8080:8080 ghcr.io/iloveitaly/pypi-api:latest
```

### Rollback by Digest
Each release tags images by candidate tag `sha-run_id-run_attempt` and digest. To pin or roll back to a specific image digest:
```bash
docker pull ghcr.io/iloveitaly/pypi-api@sha256:<IMAGE_SHA256>
docker run -d -p 8000:8000 ghcr.io/iloveitaly/pypi-api@sha256:<IMAGE_SHA256>
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8000` | Port for the HTTP server to bind to. |
| `DB_PATH` | `pypi_data.sqlite` (relative to module) | Path to the SQLite snapshot database. |
