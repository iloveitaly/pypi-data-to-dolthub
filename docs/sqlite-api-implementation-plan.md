# Finish the PyPI SQLite API and GHCR delivery

## Summary

Handoff prepared on 2026-09-15. This is an implementation plan, not a completion report. Start from the existing uncommitted server and workflow changes, preserving unrelated work.

Complete the existing FastAPI draft as a small, read-only package search service. Preserve daily SQLite and DoltHub publication, publish self-contained images to GHCR, and prepare a separate Raycast integration plan.

Original gaps identified before implementation (review current code before applying):

- `/search` needs deterministic ranking of exact, prefix, and substring matches.
- Negative limits can bypass the intended result cap.
- `/health` reports success even when the database is unavailable.
- Tests bypass startup and use a one-row fixture.
- Container publication depends on successful Dolt publication; server changes trigger the entire data refresh.
- GHCR authentication and permissions already exist.

Hosting and Raycast implementation remain outside this work.

Scope update, 2026-09-16: indexed literal contains matching is sufficient. Typo correction and fzf-style matching are out of scope; remove the unused ENABLE_SPELLFIX configuration and placeholder branch from the current implementation.

## 1. Server and search

- Keep FastAPI, SQLite, uv, and Railpack. Explicitly configure startup on `0.0.0.0`, honoring `PORT` with default `8000`; verify the generated image’s command. Current Railpack supports automatic FastAPI detection. [Railpack documentation](https://railpack.com/languages/python/)
- Resolve the default database path relative to the server module; preserve `DB_PATH` overrides. Open SQLite read-only, validate required schema and nonempty data during startup, and fail startup on invalid snapshots.
- Keep search in SQLite: do not preload package names or metadata into Python, and do not score every row through a SQL fuzzy-score function. Fetch only selected results through request-scoped, read-only connections; let SQLite manage its page cache.
- Persist normalized names (lowercase, collapsing runs of `-`, `_`, and `.` to `-`) in a separate search table keyed to project row IDs. Preserve original names in responses. Use a B-tree index for exact and prefix lookups and an FTS5 trigram index for literal substring matching. Use indexed prefix matching for one- and two-character queries; trigram searches require at least three characters. Escape FTS query syntax independently of SQL parameter binding. [SQLite FTS5 documentation](https://sqlite.org/fts5.html)
- Return deduplicated exact matches first, then prefix matches, then literal substring matches, with alphabetical ties. No approximate matching, spelling correction, or additional search extension is required.
- Use the latest stable SQLite available at implementation time, pinned by version and source checksum for reproducible builds. As checked on 2026-09-15, the official release history lists 3.53.4 (2026-07-24). Recent releases include planner improvements and FTS fixes, but do not replace the need for indexed candidate retrieval or add a general built-in fzf matcher. Recheck release notes when selecting the pin. [SQLite release history](https://sqlite.org/changes.html)
- Ensure the Python SQLite binding actually uses the selected engine; installing a newer sqlite3 CLI alone is insufficient. Provision the pinned SQLite runtime with FTS5 enabled and assert `SELECT sqlite_version()` plus a trigram MATCH probe in both database-build and server environments. No spellfix library or runtime extension loading is needed.
- Define the public contract:
  - `GET /search?q=...&limit=50`: ranked array containing `name`, `summary`, `version`, and `upload_time`.
  - Require `q`, cap it at 100 characters, and return `[]` for blank input. Validate `limit` within 1–100; invalid parameters return 422.
  - Treat query text literally; no SQL wildcards or fzf operators.
  - Preserve `/package/{name}`, adding normalized name lookup and documented response types.
  - Make `/health` verify database accessibility and search-index readiness; return 503 when unavailable.
- Add explicit response models reflecting nullable source fields. Document that snapshot versions follow upload time and are not necessarily the latest stable release.

## 2. SQLite generation and GitHub Actions

**Separate building data from distributing it.**

- Move SQLite indexing and validation into the SQLite build step, independent of Dolt import. Build the normalized-name table, B-tree index, and FTS5 trigram index once per snapshot using the pinned SQLite runtime. Optimize the FTS index and collect planner statistics before publication. Build into a temporary file and replace the output only after integrity, required-column, nonempty-table, normalized-name uniqueness, and search-index consistency checks pass. Explicitly import only the projects table into Dolt; exclude SQLite search tables and virtual-table shadow tables.
- Record snapshot timestamp, row count, and checksum alongside the database. Preserve the existing `latest` release asset name.
- Run expensive BigQuery refreshes on the daily schedule and explicit manual requests. Ordinary server pushes rebuild against the latest published SQLite asset.
- After a successful refresh, pass the same validated snapshot to independent release, container, and Dolt publication jobs. Dolt failure must remain visible without blocking the API image or SQLite release.
- Use a reusable image-publishing workflow called directly from refresh and server-push workflows; do not depend on a release event generated by `GITHUB_TOKEN`.
- Add PR CI using generated SQLite fixtures: locked dependency installation, server tests, workflow validation, and a container smoke test without publishing.
- Serialize publishing workflows with a shared concurrency group so overlapping runs cannot regress `latest`. Keep scheduled-workflow keepalive.

**Carry over the useful ZIP code example improvements.**

- Use a dedicated image-publication job with `contents: read` and `packages: write`.
- Copy the validated database into the server build context.
- Build both `linux/amd64` and `linux/arm64`.
- Keep GHCR login through `GITHUB_TOKEN` and the existing Railpack action. These patterns are present in the [ZIP code publishing workflow](https://github.com/iloveitaly/zip-code-database/blob/master/.github/workflows/publish.yml).
- Add dependency/build caching and pin the Railpack action to a reviewed commit; these are additional improvements beyond the example.

## 3. GHCR packaging and verification

- Publish to `ghcr.io/iloveitaly/pypi-data-to-dolthub`.
- Tag each build with source SHA plus workflow run ID and attempt, ensuring daily data refreshes have distinct identifiers even when code is unchanged.
- Push the candidate image, smoke-test it, then promote its digest to `latest`. Preserve the previous `latest` on failure.
- Include source revision and snapshot identity in image metadata and workflow summaries.
- Verify anonymous image pulls after configuring the GHCR package as public.
- Document local development, tests, database download, image pull/run commands, environment variables, and rollback by digest. Add a server test recipe analogous to the ZIP code project.
- Bundle the database in the image; refresh data through replacement images rather than runtime downloads.

## 4. Tests and acceptance gates

- Exercise actual FastAPI lifespan using temporary SQLite files: valid startup, missing/corrupt/empty database, missing columns, cleanup, and unavailable health checks.
- Cover exact names, case/separator normalization, prefixes, internal substrings, irrelevant queries, stable ordering, blank input, literal wildcard/FTS characters, and invalid limits. Include one- and two-character prefix inputs and digit-heavy names. Verify query plans use the intended indexes; typo correction and noncontiguous abbreviation matching are not acceptance requirements.
- Verify two consecutive SQLite builds succeed and that SQLite publication does not require Dolt success.
- Test the real container entrypoint, custom `PORT`, bundled database, pinned SQLite version, FTS5 trigram support, search, and health on both architectures. Verify ordinary projects-table queries remain usable by SQLite artifact consumers and document that indexed search requires FTS5 with the trigram tokenizer.
- Benchmark the full published snapshot before calling search production-ready. Target warm p95 below 200 ms for individual requests and below 500 ms with five concurrent searches on a documented two-vCPU environment; record cold and warm latency, memory, startup time, database/index size, and snapshot build time. Confirm Python retains no full-corpus name list. Treat misses as a release blocker requiring optimization.
- Confirm a server-only push avoids BigQuery, a scheduled refresh publishes new data, and failed candidate validation leaves `latest` unchanged.

Current evidence is source inspection only: local tests could not run offline because dependencies were unavailable, and the local database contains one fixture row.

## 5. Separate Raycast plan document

The separate handoff is [Raycast integration plan](raycast-integration-plan.md). Implement that work in the Raycast extensions repository; do not modify the extension in this repository.

Use the [current Raycast search implementation](https://github.com/raycast/extensions/blob/main/extensions/python/src/search-pypi.tsx) as the baseline. The document should specify:

- Replace DoltHub SQL requests with `/search`, mapping `summary` to the existing `description` field.
- Preserve server ranking by disabling additional client filtering.
- Debounce input, pass cancellation signals to fetch, and prevent stale responses from replacing newer results.
- Handle HTTP failures, empty input, no results, and nullable metadata explicitly.
- Retain existing package actions and the PyPI website search link.
- Require a configured public HTTPS base URL before rollout; leave hosting selection to the deployment owner.
- Include extension lint/build checks and manual rapid-typing, failure, ranking, and package-action acceptance tests.

The server plan and separate integration document should share the endpoint contract above.
