# Raycast Python extension: SQLite API integration

## Goal and prerequisites

Replace the Python extension's DoltHub SQL search request with the public package-search API described in the [server implementation plan](sqlite-api-implementation-plan.md). This document plans a separate change in [raycast/extensions](https://github.com/raycast/extensions/tree/main/extensions/python); it does not authorize implementation or publication in that repository by itself.

Prepared on 2026-09-15 from source inspection. Recheck the current extension before editing. The server and client changes have not been implemented or validated. The deployment owner must supply a public HTTPS API base URL before live verification or rollout; hosting is outside this handoff.

## API contract

- Call `GET /search?q=<URL-encoded query>&limit=50` using URLSearchParams, with no client-generated SQL.
- Expect a ranked JSON array of objects containing `name`, `summary`, `version`, and `upload_time`. Name is a string; metadata may be null. Map `summary` to the extension's existing `description`, using an empty string for missing description/version. The client does not need upload_time for its current display.
- The server requires q, caps it at 100 characters, returns an empty array for blank input, and accepts limits from 1 through 100. Invalid input produces HTTP 422.
- Server ranking is exact name, prefix, then substring, deduplicated. Preserve this order. Matching is case/separator normalized but original names are returned. Only literal contains matching is required; typo correction and fzf subsequence matching are out of scope. One- and two-character queries use prefix matching; substring matching starts at three characters.
- Snapshot versions follow upload time and are not necessarily the latest stable release. Package details continue using the existing extension behavior; this migration only changes search.

## Implementation

- Update `extensions/python/src/search-pypi.tsx` and any narrowly scoped supporting types/helpers. Remove the SQL escaping helper, Dolt response wrapper, and obsolete SQL-response example.
- Keep the public API base URL in a single configuration constant. No user credentials are required. Do not ship a placeholder URL.
- Debounce input by 300 ms and pass the debounced query as an explicit dependency to the request hook. Pass its cancellation signal to fetch and prevent out-of-order results from overwriting a newer query.
- Clear displayed results on blank input and skip that request. Reject input over 100 characters locally with a clear message rather than silently truncating it.
- Check response.ok before parsing results; surface network, HTTP, and malformed-response failures as errors rather than empty search results. Cancellation is not a user-visible failure. Keep loading, no-results, and error states distinct.
- Disable Raycast's additional list filtering so server ranking and substring matches are retained. Keep package actions and display components intact.
- Retain the link to search on pypi.org for every nonblank query, including when the API returns no results or fails. Do not silently fall back to DoltHub.
- Add a concise changelog entry explaining improved package search and the backend change.

## Acceptance and rollout

- Run the extension's existing lint and build scripts. Add focused tests using existing test infrastructure, if present, for request construction, response mapping, and failure handling.
- Manually verify exact, prefix, substring, case/separator, and one-/two-character searches against the live API. Confirm server order is preserved.
- Verify rapid typing and clearing input cannot show stale results; exercise an offline request, HTTP failure, malformed JSON, null metadata, no matches, and overlong input.
- Verify all existing package actions and the PyPI search link still work.
- Submit the extension change as a separate PR after the service is live and server acceptance gates pass. Record the tested API URL, server image digest/snapshot, lint/build results, and live checks in the PR description. Submission and publication require the integration owner's authorization.
