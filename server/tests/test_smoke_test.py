"""Tests for smoke_test probe derivation, expanded catalogs, and architecture validation."""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "scripts"))

from build_search_index import process_database
from smoke_test import SnapshotProbes, normalize_arch, run_smoke_tests


def create_indexed_catalog(db_path: Path, packages: list[str]) -> None:
    raw_path = db_path.with_suffix(".raw.sqlite")
    meta_path = db_path.with_suffix(".meta.json")

    conn = sqlite3.connect(raw_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE projects (
            id INTEGER,
            name TEXT NOT NULL,
            version TEXT,
            author TEXT,
            author_email TEXT,
            home_page TEXT,
            license TEXT,
            maintainer TEXT,
            maintainer_email TEXT,
            package_url TEXT,
            platform TEXT,
            project_url TEXT,
            requires_python TEXT,
            summary TEXT,
            upload_time TEXT,
            yanked INTEGER DEFAULT 0,
            yanked_reason TEXT,
            classifiers TEXT,
            requires_dist TEXT
        )
    """)
    for pkg in packages:
        cur.execute(
            "INSERT INTO projects (name, version, summary, upload_time) VALUES (?, '1.0.0', 'Test', '2026-01-01T00:00:00Z')",
            (pkg,),
        )
    conn.commit()
    conn.close()

    process_database(raw_path, db_path, meta_path)
    raw_path.unlink()
    if meta_path.exists():
        meta_path.unlink()


def test_normalize_arch():
    assert normalize_arch("x86_64") == "x86_64"
    assert normalize_arch("amd64") == "x86_64"
    assert normalize_arch("x64") == "x86_64"
    assert normalize_arch("aarch64") == "aarch64"
    assert normalize_arch("arm64") == "aarch64"
    assert normalize_arch("arm64v8") == "aarch64"


def test_snapshot_probes_on_expanded_catalog_with_five_dantic_prefixes(tmp_path):
    """
    Catalog contains 5 dantic-* prefix packages and pydantic.
    SnapshotProbes must find a probe where prefix matches do not saturate the limit,
    and internal substring matching is correctly exercised.
    """
    catalog_db = tmp_path / "expanded.sqlite"
    packages = [
        "dantic-alpha",
        "dantic-beta",
        "dantic-gamma",
        "dantic-delta",
        "dantic-epsilon",
        "pydantic",
        "pydantic-core",
        "requests",
        "requests-mock",
        "urllib3",
    ]
    create_indexed_catalog(catalog_db, packages)

    probes = SnapshotProbes(catalog_db)
    assert probes.sub_query is not None
    # If sub_query happens to match prefixes, it must NOT saturate limit=5
    assert len(probes.expected_prefix_matches) < 5
    # Substring probe must expect at least one internal substring match
    assert len(probes.expected_sub_matches) >= 1

    # Verify that the probe's token actually appears as an internal substring
    for match in probes.expected_sub_matches:
        assert probes.sub_query in match
        assert not match.startswith(probes.sub_query)


def test_run_smoke_tests_with_mocked_server(tmp_path):
    """Verify run_smoke_tests against an expanded catalog with 5 dantic-* packages."""
    catalog_db = tmp_path / "catalog.sqlite"
    packages = [
        "dantic-1",
        "dantic-2",
        "dantic-3",
        "dantic-4",
        "dantic-5",
        "pydantic",
        "pydantic-core",
        "requests",
        "tool-helper",
    ]
    create_indexed_catalog(catalog_db, packages)

    probes = SnapshotProbes(catalog_db)

    # Mock server responses that simulate a correct search API
    def mock_make_request(url: str):
        if "/health" in url:
            return 200, {"status": "ok", "sqlite_version": "3.53.1", "arch": "x86_64"}
        if "/package/requests" in url:
            return 200, {"name": "requests", "version": "1.0.0"}
        if "/package/nonexistent" in url:
            return 404, {"detail": "Not found"}
        if "/search?" in url:
            if "limit=0" in url or "limit=101" in url:
                return 422, {"detail": "Validation error"}
            if "q=requests" in url:
                return 200, [
                    {
                        "name": "requests",
                        "version": "1.0.0",
                        "summary": "s",
                        "upload_time": "u",
                    }
                ]
            if f"q={probes.char1_query}" in url:
                return 200, [
                    {
                        "name": "dantic-1",
                        "version": "1.0.0",
                        "summary": "s",
                        "upload_time": "u",
                    }
                ]
            if f"q={probes.char2_query}" in url:
                return 200, [
                    {
                        "name": "dantic-1",
                        "version": "1.0.0",
                        "summary": "s",
                        "upload_time": "u",
                    }
                ]
            if f"q={probes.sub_query}" in url:
                # Return prefix matches followed by substring matches
                results = []
                for p in probes.expected_prefix_matches:
                    results.append(
                        {
                            "name": p,
                            "version": "1.0.0",
                            "summary": "s",
                            "upload_time": "u",
                        }
                    )
                for s in probes.expected_sub_matches:
                    results.append(
                        {
                            "name": s,
                            "version": "1.0.0",
                            "summary": "s",
                            "upload_time": "u",
                        }
                    )
                return 200, results
            # Unicode or other queries
            return 200, []
        return 404, {}

    with (
        patch(
            "smoke_test.wait_for_ready",
            return_value={"status": "ok", "sqlite_version": "3.53.1", "arch": "x86_64"},
        ),
        patch("smoke_test.make_request", side_effect=mock_make_request),
    ):
        # Should pass without assertion errors
        run_smoke_tests(
            base_url="http://localhost:8000",
            expected_sqlite="3.53.1",
            expected_arch="amd64",  # verifies normalization amd64 -> x86_64
            db_path=catalog_db,
        )


def test_run_smoke_tests_fails_on_architecture_mismatch():
    """Smoke test must fail if container architecture does not match expected_arch."""
    with patch(
        "smoke_test.wait_for_ready",
        return_value={"status": "ok", "sqlite_version": "3.53.1", "arch": "x86_64"},
    ):
        with pytest.raises(AssertionError, match="Architecture mismatch"):
            run_smoke_tests(
                base_url="http://localhost:8000",
                expected_sqlite="3.53.1",
                expected_arch="aarch64",  # Mismatch: got x86_64
            )


def test_run_smoke_tests_fails_when_substring_disabled(tmp_path):
    """Smoke test must fail if substring matching is disabled and returns no internal substring matches."""
    catalog_db = tmp_path / "catalog.sqlite"
    create_indexed_catalog(catalog_db, ["pydantic", "requests", "core-tool"])

    def mock_make_request(url: str):
        if "/health" in url:
            return 200, {"status": "ok", "sqlite_version": "3.53.1", "arch": "x86_64"}
        if "/package/" in url:
            return 200, {"name": "requests", "version": "1.0.0"}
        if "/search?" in url:
            if "limit=0" in url or "limit=101" in url:
                return 422, {"detail": "Validation error"}
            # Return empty or only prefix matches when substring search is tested
            return 200, []
        return 404, {}

    with (
        patch(
            "smoke_test.wait_for_ready",
            return_value={"status": "ok", "sqlite_version": "3.53.1", "arch": "x86_64"},
        ),
        patch("smoke_test.make_request", side_effect=mock_make_request),
    ):
        with pytest.raises(AssertionError):
            run_smoke_tests(
                base_url="http://localhost:8000",
                db_path=catalog_db,
            )
