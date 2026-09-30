#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "click>=8.1.8",
# ]
# ///
"""
Container and API smoke-test script.
Validates startup, custom PORT, health status, container architecture, SQLite runtime version,
exact matching, prefix matching, FTS5 trigram substring matching,
ranking, pagination limits, and Unicode resilience.
"""

import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import click

# Default port for local smoke testing
DEFAULT_PORT = 8000

# Pinned SQLite version expected in runtime container
PINNED_SQLITE_VERSION = "3.53.1"

# Maximum polling retries for container readiness
DEFAULT_RETRIES = 30

# PEP 503 package name separator pattern
NAME_SEPARATOR_REGEX = re.compile(r"[-_.]+")


def normalize_name(name: str) -> str:
    """PEP 503 normalization: lowercase, collapse runs of '-', '_', '.' to '-'."""
    if not name:
        return ""
    return NAME_SEPARATOR_REGEX.sub("-", name.strip()).lower()


def normalize_arch(arch: str) -> str:
    """Normalize common architecture names for reliable comparison."""
    a = arch.lower().strip()
    if a in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if a in ("aarch64", "arm64", "arm64v8"):
        return "aarch64"
    return a


class SnapshotProbes:
    """
    Derive catalog-independent search probes and expected ranking from a SQLite snapshot
    using standard SQLite queries (not the server's FTS5 implementation).
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._derive_probes()

    def _derive_probes(self):
        conn = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
        try:
            cur = conn.cursor()

            # 1. Exact match probe: prefer 'requests' if present, otherwise first available package
            cur.execute(
                """
                SELECT p.name, s.normalized_name
                FROM projects_search s
                JOIN projects p ON p.rowid = s.project_rowid
                WHERE s.normalized_name = 'requests'
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    """
                    SELECT p.name, s.normalized_name
                    FROM projects_search s
                    JOIN projects p ON p.rowid = s.project_rowid
                    ORDER BY s.project_rowid LIMIT 1
                    """
                )
                row = cur.fetchone()
            if not row:
                raise RuntimeError(
                    f"Database at {self.db_path} contains no projects_search rows."
                )
            self.exact_name, self.exact_norm = row[0], row[1]

            # 2. 1-character prefix probe
            cur.execute(
                "SELECT normalized_name FROM projects_search WHERE length(normalized_name) = 1 LIMIT 1"
            )
            r1 = cur.fetchone()
            if r1:
                self.char1_query = r1[0]
                self.char1_has_exact = True
            else:
                cur.execute(
                    "SELECT substr(normalized_name, 1, 1) FROM projects_search WHERE length(normalized_name) >= 1 LIMIT 1"
                )
                self.char1_query = cur.fetchone()[0]
                self.char1_has_exact = False

            # 3. 2-character prefix probe
            cur.execute(
                "SELECT normalized_name FROM projects_search WHERE length(normalized_name) = 2 LIMIT 1"
            )
            r2 = cur.fetchone()
            if r2:
                self.char2_query = r2[0]
                self.char2_has_exact = True
            else:
                cur.execute(
                    "SELECT substr(normalized_name, 1, 2) FROM projects_search WHERE length(normalized_name) >= 2 LIMIT 1"
                )
                self.char2_query = cur.fetchone()[0]
                self.char2_has_exact = False

            # 4. Internal substring probe (length >= 3)
            # Find a token that exercises internal substring matching (starts at index >= 1),
            # where prefix matches do not exhaust the requested result limit (5).
            self.sub_query = None
            self.expected_prefix_matches = []
            self.expected_sub_matches = []

            cur.execute(
                "SELECT normalized_name FROM projects_search WHERE length(normalized_name) >= 5 LIMIT 100"
            )
            sample_names = [r[0] for r in cur.fetchall()]

            for name in sample_names:
                for start in range(1, len(name) - 2):
                    for token_len in (3, 4, 5):
                        if start + token_len > len(name):
                            continue
                        token = name[start : start + token_len]
                        if token.startswith("-") or token.endswith("-"):
                            continue

                        # Reference query: how many packages match token as a prefix?
                        cur.execute(
                            "SELECT normalized_name FROM projects_search WHERE normalized_name LIKE ? ORDER BY normalized_name ASC LIMIT 5",
                            (f"{token}%",),
                        )
                        p_matches = [r[0] for r in cur.fetchall()]
                        if len(p_matches) >= 5:
                            # Prefix matches would saturate limit=5, leaving no slot for substring matches
                            continue

                        # Reference query: how many packages match token as an internal substring?
                        cur.execute(
                            "SELECT normalized_name FROM projects_search WHERE normalized_name LIKE ? AND normalized_name NOT LIKE ? ORDER BY normalized_name ASC LIMIT 5",
                            (f"%{token}%", f"{token}%"),
                        )
                        s_matches = [r[0] for r in cur.fetchall()]
                        if s_matches:
                            self.sub_query = token
                            self.expected_prefix_matches = p_matches
                            self.expected_sub_matches = s_matches
                            break
                    if self.sub_query:
                        break
                if self.sub_query:
                    break

            if not self.sub_query:
                # Fallback if catalog is minimal
                self.sub_query = "test"
                self.expected_prefix_matches = []
                self.expected_sub_matches = []

        finally:
            conn.close()


def make_request(url: str, timeout: float = 20.0) -> tuple[int, dict | list | str]:
    req = urllib.request.Request(url, headers={"User-Agent": "PyPI-SmokeTest/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body
            return resp.status, data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = body
        return e.code, data


def wait_for_ready(base_url: str, retries: int = 30, delay: float = 1.0) -> dict:
    health_url = f"{base_url}/health"
    print(f"Waiting for server readiness at {health_url}...")
    for i in range(retries):
        try:
            status, data = make_request(health_url)
            if status == 200 and isinstance(data, dict) and data.get("status") == "ok":
                print(f"Server ready after {i + 1} attempts.")
                return data
        except Exception:
            pass
        time.sleep(delay)
    raise TimeoutError(
        f"Server did not become ready at {health_url} within {retries * delay}s"
    )


def run_smoke_tests(
    base_url: str,
    expected_sqlite: str | None = None,
    expected_arch: str | None = None,
    db_path: Path | None = None,
    retries: int = DEFAULT_RETRIES,
) -> None:
    print(f"Starting smoke tests against {base_url}...")

    # 1. Health check, SQLite version, and container architecture verification
    health_data = wait_for_ready(base_url, retries=retries)
    assert health_data.get("status") == "ok", f"Health status not ok: {health_data}"
    active_sqlite = health_data.get("sqlite_version")
    print(f"Active SQLite runtime reported by /health: {active_sqlite}")
    if expected_sqlite:
        assert active_sqlite == expected_sqlite, (
            f"SQLite version mismatch: expected {expected_sqlite}, got {active_sqlite}"
        )

    active_arch = health_data.get("arch")
    print(f"Active container architecture reported by /health: {active_arch}")
    if expected_arch:
        assert active_arch, "Endpoint /health did not report 'arch'"
        assert normalize_arch(active_arch) == normalize_arch(expected_arch), (
            f"Architecture mismatch: expected {expected_arch} (normalized {normalize_arch(expected_arch)}), "
            f"got {active_arch} (normalized {normalize_arch(active_arch)})"
        )
        print(f"✓ Container architecture verified: {active_arch}")

    # Derive probes if database is available
    probes = None
    if db_path and db_path.is_file():
        print(f"Deriving catalog probes from snapshot: {db_path}...")
        probes = SnapshotProbes(db_path)
    else:
        for candidate in [Path("server/pypi_data.sqlite"), Path("pypi_data.sqlite")]:
            if candidate.is_file():
                print(
                    f"Auto-detected snapshot database at {candidate}. Deriving probes..."
                )
                probes = SnapshotProbes(candidate)
                break

    exact_query = probes.exact_norm if probes else "requests"
    exact_expected = probes.exact_name if probes else "requests"

    # 2. Exact match search
    code, data = make_request(f"{base_url}/search?q={exact_query}&limit=5")
    assert code == 200, f"/search?q={exact_query} failed with HTTP {code}: {data}"
    assert isinstance(data, list) and len(data) >= 1, (
        f"Expected non-empty list for '{exact_query}': {data}"
    )
    assert normalize_name(data[0]["name"]) == normalize_name(exact_expected), (
        f"Exact match '{exact_expected}' must rank #1, got: {data[0]['name']}"
    )
    for required_key in ("name", "version", "summary", "upload_time"):
        assert required_key in data[0], (
            f"Missing key {required_key} in search result: {data[0]}"
        )
    print("✓ Exact match search verified.")

    # 3. Short prefix searches (1 and 2 characters)
    char1_q = probes.char1_query if probes else "a"
    code, data = make_request(f"{base_url}/search?q={char1_q}&limit=5")
    assert code == 200, f"/search?q={char1_q} failed with HTTP {code}: {data}"
    assert isinstance(data, list) and len(data) >= 1, (
        f"Expected non-empty results for '{char1_q}'"
    )
    if probes and probes.char1_has_exact:
        assert normalize_name(data[0]["name"]) == char1_q, (
            f"Exact match '{char1_q}' must rank #1, got: {data[0]['name']}"
        )
    for item in data:
        assert normalize_name(item["name"]).startswith(char1_q), (
            f"Result '{item['name']}' does not start with prefix '{char1_q}'"
        )

    char2_q = probes.char2_query if probes else "ab"
    code, data = make_request(f"{base_url}/search?q={char2_q}&limit=5")
    assert code == 200, f"/search?q={char2_q} failed with HTTP {code}: {data}"
    assert isinstance(data, list) and len(data) >= 1, (
        f"Expected non-empty results for '{char2_q}'"
    )
    if probes and probes.char2_has_exact:
        assert normalize_name(data[0]["name"]) == char2_q, (
            f"Exact match '{char2_q}' must rank #1, got: {data[0]['name']}"
        )
    for item in data:
        assert normalize_name(item["name"]).startswith(char2_q), (
            f"Result '{item['name']}' does not start with prefix '{char2_q}'"
        )
    print("✓ Short prefix search (1 & 2 chars) verified.")

    # 4. Independent substring probe and ranking verification
    sub_q = probes.sub_query if probes else "dantic"
    code, data = make_request(f"{base_url}/search?q={sub_q}&limit=5")
    assert code == 200, f"/search?q={sub_q} failed with HTTP {code}: {data}"
    assert isinstance(data, list) and len(data) >= 1, (
        f"Expected non-empty list for '{sub_q}': {data}"
    )

    returned_names = [normalize_name(item["name"]) for item in data]
    for n in returned_names:
        assert sub_q in n, f"Result '{n}' does not contain query token '{sub_q}'"

    # Verify ranking: any prefix matches must precede all internal substring matches
    saw_internal_substring = False
    has_internal_substring_in_results = False
    for n in returned_names:
        if n.startswith(sub_q):
            assert not saw_internal_substring, (
                f"Ranking violation: prefix match '{n}' appeared after internal substring match in {returned_names}"
            )
        else:
            saw_internal_substring = True
            has_internal_substring_in_results = True

    if probes and probes.expected_sub_matches:
        assert has_internal_substring_in_results, (
            f"Substring search verification failed: probe '{sub_q}' expected internal substring matches "
            f"but none were returned: {returned_names}"
        )
    print("✓ Substring and ranking verification verified.")

    # 5. Result limit & validation checks
    code, data = make_request(f"{base_url}/search?q={exact_query}&limit=2")
    assert code == 200, f"/search with limit=2 failed: {code}"
    assert len(data) <= 2, f"Expected <= 2 results for limit=2, got {len(data)}"

    code, _ = make_request(f"{base_url}/search?q={exact_query}&limit=0")
    assert code == 422, f"limit=0 must return 422, got HTTP {code}"

    code, _ = make_request(f"{base_url}/search?q={exact_query}&limit=101")
    assert code == 422, f"limit=101 must return 422, got HTTP {code}"
    print("✓ Parameter validation and limits verified.")

    # 6. Package details endpoint
    code, data = make_request(f"{base_url}/package/{exact_expected}")
    assert code == 200, f"/package/{exact_expected} failed with HTTP {code}"
    assert data["name"] == exact_expected, (
        f"Expected package name '{exact_expected}', got {data.get('name')}"
    )

    code, _ = make_request(f"{base_url}/package/nonexistent-package-xyz-12345")
    assert code == 404, f"Nonexistent package must return 404, got HTTP {code}"
    print("✓ Package details endpoint verified.")

    # 7. Unicode resilience (surrogate boundary U+D7FF, private use U+E000, max codepoint U+10FFFF)
    for unicode_query in ["\ud7ff", "\ue000", "\U0010ffff"]:
        encoded_q = urllib.parse.quote(unicode_query)
        code, data = make_request(f"{base_url}/search?q={encoded_q}&limit=5")
        assert code == 200, (
            f"Unicode query ({repr(unicode_query)}) failed with HTTP {code}: {data}"
        )
        assert isinstance(data, list), (
            f"Expected list response for Unicode query: {data}"
        )
    print("✓ Unicode input resilience (U+D7FF, U+E000, U+10FFFF) verified.")


@click.command()
@click.option(
    "--url",
    default=None,
    help="Base URL of the server (e.g. http://localhost:8000).",
)
@click.option(
    "--port",
    type=int,
    default=DEFAULT_PORT,
    help=f"Server port (defaults to {DEFAULT_PORT}).",
)
@click.option(
    "--expected-sqlite-version",
    default=PINNED_SQLITE_VERSION,
    help=f"Assert expected SQLite version from /health (e.g. {PINNED_SQLITE_VERSION}).",
)
@click.option(
    "--expected-arch",
    default=None,
    help="Assert expected container architecture from /health (e.g. x86_64, aarch64).",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to snapshot database for deriving catalog probes.",
)
@click.option(
    "--retries",
    type=int,
    default=DEFAULT_RETRIES,
    help=f"Readiness poll retries (defaults to {DEFAULT_RETRIES}).",
)
def cli(
    url: str | None,
    port: int,
    expected_sqlite_version: str,
    expected_arch: str | None,
    db_path: Path | None,
    retries: int,
):
    """Smoke test PyPI Search API server."""
    base_url = url.rstrip("/") if url else f"http://localhost:{port}"
    resolved_db = db_path.resolve() if db_path else None

    try:
        run_smoke_tests(
            base_url=base_url,
            expected_sqlite=expected_sqlite_version,
            expected_arch=expected_arch,
            db_path=resolved_db,
            retries=retries,
        )
        click.echo(f"\nAll smoke tests PASSED successfully against {base_url}!")
    except Exception as e:
        click.echo(f"\nSmoke test FAILED: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
