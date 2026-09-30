# Implementation review: remaining fixes

Reviewed 2026-09-17. This is an implementation handoff, not a completion report. Preserve existing working-tree changes and unrelated work.

## Goal and scope

Finish the SQLite package-search API and GHCR publishing cleanup. Search remains literal, case/separator-normalized matching: exact names first, prefixes second, then internal substrings, with deterministic alphabetical ties. One- and two-character queries use prefix matching; substring matching starts at three characters. Fuzzy matching is out of scope.

The workflow dependency graph and FTS validation have improved. Address the five findings below before considering the implementation complete. See also the [cleanup plan](sqlite-api-cleanup-plan.md) and [Raycast handoff](raycast-integration-plan.md).

## 1. [P1] Preserve published snapshots until preparation finishes

**Location:** `scripts/prepare_snapshot.py`, final output handling around lines 152–158.

**Problem:** `shutil.copyfile()` overwrites the existing database and metadata before gzip compression finishes. Compression writes directly to the final gzip path. A simulated compression failure changed all three previous outputs and left an incomplete gzip. The existing atomic-replacement test covers `process_database()`, not this outer preparation function.

**Fix:**

- Prepare the database, metadata, and compressed archive entirely in a temporary directory on the destination filesystem.
- Finish compression and verify the staged artifacts before replacing any published output.
- Use atomic file replacement rather than copying into final paths. Do not describe several independent replacements as a single atomic bundle operation; either provide rollback for replacement failures or publish a versioned bundle through one atomic pointer switch.
- Preserve the existing filenames expected by artifact upload and local consumers, or update all consumers together if introducing a bundle pointer.

**Acceptance:**

- Add tests for `prepare_snapshot()` using temporary fixtures and mocked downloads; no BigQuery or network calls are required.
- Inject failures during copy, compression, validation, and final replacement. Existing valid outputs must remain usable and mutually consistent after handled failures.
- Successful preparation produces a gzip that decompresses to the database described by the metadata checksum.

## 2. [P2] Make production smoke assertions independent of fixture ranking

**Location:** `scripts/smoke_test.py`, substring assertion around lines 82–88.

**Problem:** The smoke test requires `pydantic` among the first five results for `dantic`. The server correctly ranks prefix matches before substring matches. Adding five valid `dantic-*` packages makes the smoke test fail despite correct API behavior.

**Fix:**

- Keep fixed fixture-name and ranking expectations in fixture tests.
- For production smoke tests, derive a substring probe and expected results from the prepared snapshot using an independent reference query, not the API's FTS implementation.
- Ensure the probe actually exercises internal-substring matching rather than filling the result limit entirely with exact/prefix matches.
- Validate HTTP status, response shape, result limits, normalization, and expected ordering.
- Apply the same approach to short-query assertions that currently assume specific fixture packages such as `a` and `ab` exist.

**Acceptance:**

- Smoke tests pass on the existing fixture and a valid expanded catalog containing at least five `dantic-*` prefix matches.
- They still fail when substring search is deliberately disabled or returns incorrect results.
- Candidate verification uses the same prepared snapshot that was bundled into the image.

## 3. [P2] Select and verify the smoke-test architecture explicitly

**Locations:** `.github/workflows/publish.yml`, ARM64 smoke step around lines 171–177; `compose.yaml`.

**Problem:** Pulling an ARM64 image does not explicitly select ARM64 for the following Compose container creation. With multiple variants available locally, the current steps do not reliably prove which architecture ran.

**Fix:**

- Add an explicit Compose platform setting that can be supplied by each smoke step, with a sensible local default or optional configuration.
- Set `linux/amd64` for the AMD64 step and `linux/arm64` for the ARM64 step.
- Inspect the running container's image architecture or execute an architecture probe before accepting the test result.
- Keep failure logs and unconditional teardown. Promote the multi-platform candidate only after both architecture checks and endpoint tests pass.

**Acceptance:**

- On the AMD64 GitHub runner, prove that the ARM64 step executes the ARM64 variant under emulation.
- A failure on either architecture prevents updating `latest`.

Reference: [Docker Compose platform documentation](https://docs.docker.com/reference/compose-file/services/#platform).

## 4. [P2] Bundle the selected snapshot in local image builds

**Location:** `Justfile`, `docker` recipe around lines 98–100.

**Problem:** `just build_sqlite` writes the snapshot at the repository root, while `just docker` builds the `server/` context without copying it there. The server directory currently contains a small fixture database, so a successful local image build can package stale/sample data after a refresh.

**Fix:**

- Make local image preparation explicitly select the root snapshot, copy its database and metadata into the server context, and validate them before invoking Railpack.
- Fail clearly if the selected snapshot is missing or its checksum is invalid. Do not silently fall back to an existing server fixture.
- Share this preparation behavior with CI where practical, without requiring a BigQuery refresh just to build an image.

**Acceptance:**

- Place different valid snapshots at the root and in `server/`, then verify the built image serves the selected root snapshot.
- Missing or invalid root snapshots fail before image construction.
- Metadata in the image identifies the snapshot actually bundled.

## 5. [P2] Handle the Unicode surrogate boundary in prefix bounds

**Location:** `server/main.py`, `get_prefix_bounds()` around lines 88–92.

**Problem:** U+D7FF is valid input, but incrementing it creates U+D800, a surrogate that Python cannot encode as a SQLite text parameter. An API request containing U+D7FF reproduces HTTP 500 despite the existing maximum-codepoint regression test.

**Fix:**

- Calculate the next valid Unicode scalar value, skipping U+D800 through U+DFFF.
- Preserve existing handling for prefixes ending in the maximum scalar value and for prefixes without a finite upper bound.
- Preserve indexed range lookup for ordinary package-name queries.

**Acceptance:**

- Add API and helper regression cases for U+D7FF, U+E000, U+10FFFF, and mixed prefixes ending with these characters.
- Valid Unicode queries return the documented response rather than HTTP 500; ordinary exact/prefix ranking is unchanged.

## Additional cleanup

### Consistent SQLite version policy

Runtime verification currently accepts 3.53.1 or newer, smoke tests require exactly 3.53.1, and unused source constants reference 3.53.4. Select the latest stable SQLite at implementation time, provision it reproducibly, and use one shared version policy across the builder, server, smoke tests, and documentation. Verify the engine Python actually loads on both architectures. Remove unused source URL/checksum constants if they are not part of provisioning.

### Temporary DuckDB files

Snapshot preparation creates a temporary DuckDB directory, but `build_latest_sqlite.sql` still points at the repository-root `duckdb_temp`. Configure DuckDB to use the actual temporary workspace, and ensure cleanup on success and failure. Pass SQL via subprocess input rather than shell redirection so paths containing spaces do not break invocation.

### Publication evidence

Report the actual promoted manifest digest and prepared snapshot checksum in the workflow summary. Use the verified runtime version instead of a hardcoded string. Distinguish a successful promotion from a failed attempt in summaries emitted with `always()`.

## Validation baseline and limits

- Existing server/index-builder suite: **34 tests passed** during review.
- Ruff lint and formatting checks passed using the existing server environment.
- Snapshot overwrite, catalog-dependent smoke failure, and Unicode HTTP 500 were reproduced with temporary fixtures.
- Containers were not built or published, and the new workflows were not run live during this review.
- No implementation source files were changed as part of the review.

## Suggested execution order

1. Fix final snapshot publication and add failure-injection tests.
2. Correct production smoke assertions and explicitly test both architectures.
3. Fix local image snapshot selection and Unicode prefix bounds.
4. Consolidate runtime configuration, temporary-file handling, and publication summaries.
5. Run lint and the complete test suite, then validate both container architectures and both workflow preparation paths. Record local results separately from live GHA results.
