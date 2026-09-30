# SQLite API and GitHub Actions cleanup

Prepared 2026-09-16 following review of the current uncommitted implementation. This is a cleanup plan, not a completion report. Preserve unrelated working-tree changes.

## Goal

Simplify publishing and make indexed package-name contains search reliable. Exact matches rank first, prefixes second, and internal substrings third. Typo correction and fzf-style matching are out of scope. Use prefix matching for one- and two-character queries; indexed substring matching starts at three characters.

This document refines the [server implementation plan](sqlite-api-implementation-plan.md). The separate [Raycast integration plan](raycast-integration-plan.md) remains a follow-up; hosting and extension implementation are outside this cleanup.

## 1. Simplify GitHub Actions

Keep `ci.yml` and one `publish.yml`, with this publishing structure:

```text
prepare_snapshot
  ├─ publish_container
  ├─ publish_release   [data refresh only]
  └─ publish_dolt      [data refresh only]
```

- Always run `prepare_snapshot`. Refresh from BigQuery on scheduled runs or explicit manual refreshes; otherwise download the published SQLite snapshot.
- Converge both paths on the same index-building and validation code. Rebuild search indexes from the current code so existing releases without search tables and subsequent search-schema changes work without another BigQuery query.
- Produce one compressed snapshot plus metadata as the workflow artifact. Downstream jobs download and decompress it as needed; do not transfer both compressed and uncompressed database copies.
- Expose a single refresh output for conditional release and Dolt jobs. Container publication depends only on successful preparation. Remove skipped-job handling and the long `always()` condition.
- Keep Dolt publication independent of container and release publication. Failures remain visible without blocking the other destinations.
- Move inline Python validation and duplicated container smoke-test commands into shared scripts that also run locally. Keep workflow YAML focused on orchestration.
- Split dependency setup: SQLite preparation does not install Dolt; Dolt publication does not install BigQuery dependencies. Remove pandas/pyarrow from fixture generation, which does not use them.
- Include shared scripts, index-building code, and Compose configuration in relevant workflow triggers.
- Preserve GHCR candidate promotion: build a uniquely tagged candidate, test it, then promote its manifest digest to `latest`. Leave the existing `latest` unchanged on failure. Retain serialized publication and scheduled-workflow keepalive.

## 2. Finish contains search cleanly

- Delete `ENABLE_SPELLFIX`, its placeholder query branch, and associated documentation. No fuzzy-search dependencies or custom native search extensions are needed.
- Retain case/separator normalization, deduplication, exact/prefix/substring ranking, and deterministic alphabetical ties.
- Keep the B-tree lookup for exact/prefix queries and FTS5 trigram lookup for internal substrings. Do not preload package names into Python or introduce whole-table scoring.
- Remove the redundant normalized-name index: the existing `UNIQUE` constraint already provides one.
- Fix prefix-bound calculation for maximum Unicode code points so arbitrary query input cannot produce an HTTP 500. Preserve the documented input contract and indexed lookup for ordinary package-name queries.

## 3. Fix validation and SQLite provisioning

- Correct FTS validation. An empty FTS index currently passes generation, startup, and health checks because external-content table reads do not prove index readiness.
- During generation, use the content-aware FTS5 integrity check: `INSERT INTO projects_fts(projects_fts, rank) VALUES('integrity-check', 1)`. At runtime, use an actual `MATCH` probe against a known indexed name; do not run write-style integrity commands on read-only connections.
- Provision the latest stable SQLite available at implementation time, pinning the version and source checksum for reproducibility. Share the selected runtime between indexing, tests, and the server. Assert the version Python actually uses and verify FTS5 trigram support; a newer SQLite CLI alone is insufficient.
- Test the production index-building function rather than separately reconstructing its schema in fixtures. Cover rebuilding an existing index and adding indexes to a legacy projects-only snapshot.
- Make the complete snapshot build repeatable and atomic. Build raw data and indexes in a temporary workspace, validate them, then replace the published database. A failed rebuild must preserve the previous valid output; atomic indexing alone is insufficient.
- Verify downloaded snapshot checksums when metadata is present. Explicitly support the initial legacy release without metadata by rebuilding and validating it, then generating metadata for the prepared snapshot. Subsequent published snapshots must carry checksums.
- Include snapshot timestamp, checksum, search-schema version, and SQLite version in metadata. Record snapshot identity and the promoted image digest in publication output.

## 4. Tighten CI and container checks

- Require passing server and index-builder tests before publication; a separate PR-only CI workflow is not a publication gate. Share the test command between CI and publishing.
- Smoke-test both AMD64 and ARM64 candidate images before promoting the multi-platform manifest. Verify actual startup, custom `PORT`, the bundled snapshot, and the selected SQLite runtime.
- Replace `grep requests` checks with successful HTTP-status and parsed JSON assertions for exact and internal-substring searches, including ranking and result limits.
- Capture container logs on failure and clean up containers unconditionally.
- Exercise both snapshot paths: scheduled/manual refresh and server-only publication using an existing release. Confirm Dolt failure does not block the other publishers and candidate failure preserves `latest`.

## 5. Documentation and acceptance

- Replace unconditional sub-5ms claims with measured results and their dataset, runtime, hardware, and test conditions. Fix the README's machine-specific `file://` link.
- Document short-query prefix behavior and the absence of typo correction.
- Retain the full-dataset performance targets from the implementation plan: warm p95 below 200 ms and p95 below 500 ms with five concurrent requests on a documented two-vCPU environment. Record memory and startup time as well as latency.
- Add regression coverage for empty/stale FTS indexes, legacy snapshots, repeated builds, failed-build preservation, Unicode input, and both image architectures.

### Review evidence and limits

- The existing 26 server tests passed during review.
- The local snapshot contained 1,035,371 packages and was approximately 1.11 GiB.
- A short in-process benchmark measured about 5.3 ms warm p95 and 13.8 ms p95 with five concurrent requests. This is not deployed-container validation or a controlled two-vCPU benchmark.
- Local Python and snapshot metadata reported SQLite 3.50.4; the requested runtime pin was not implemented.
- Empty-FTS false-positive health checks and a Unicode-triggered HTTP 500 were reproduced using temporary fixtures.
- The proposed GHA workflows and container execution were not live-validated during review.

## Implementation order

1. Simplify snapshot preparation and the workflow dependency graph.
2. Fix FTS validation, repeatable builds, and SQLite runtime consistency; remove unused spellfix code.
3. Tighten CI and both-architecture publication checks.
4. Refresh documentation and record the resulting validation evidence.
