import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure root and scripts are in sys.path
repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "scripts"))
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "server"))

from build_search_index import (
    build_indexes,
    create_fixture_database,
    process_database,
    validate_source_projects,
    verify_database,
    verify_snapshot_checksum,
)
from main import validate_db
from sqlite_runtime import PINNED_SQLITE_VERSION, verify_sqlite_runtime


def test_sqlite_runtime_verification():
    """Verify that current SQLite runtime meets pinned version and supports FTS5 trigram."""
    ver = verify_sqlite_runtime()
    assert ver >= PINNED_SQLITE_VERSION


def test_build_indexes_and_verify(tmp_path):
    """Test building search indexes on a fresh database using production build_indexes."""
    db_path = tmp_path / "fresh.sqlite"
    create_fixture_database(db_path)

    conn = sqlite3.connect(db_path)
    count = validate_source_projects(conn)
    assert count == 20

    build_indexes(conn)
    verify_database(conn)

    # Verify search table and FTS5 table exist and are queryable
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM projects_search")
    assert cursor.fetchone()[0] == 20

    cursor.execute("SELECT 1 FROM projects_fts WHERE projects_fts MATCH '\"requests\"'")
    assert cursor.fetchone() is not None
    conn.close()


def test_rebuild_existing_index(tmp_path):
    """Test rebuilding search indexes on an already indexed database."""
    db_path = tmp_path / "rebuild.sqlite"
    create_fixture_database(db_path)

    conn = sqlite3.connect(db_path)
    build_indexes(conn)
    verify_database(conn)

    # Rebuild a second time
    build_indexes(conn)
    verify_database(conn)

    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM projects_search")
    assert cursor.fetchone()[0] == 20
    conn.close()


def test_legacy_snapshot_indexing(tmp_path):
    """Test adding search indexes to a legacy database containing only the projects table."""
    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE projects (
            name TEXT NOT NULL,
            version TEXT,
            summary TEXT,
            upload_time TEXT
        )
    """)
    conn.execute(
        "INSERT INTO projects VALUES ('legacy-pkg', '1.0.0', 'A legacy package', '2022-01-01')"
    )
    conn.commit()
    conn.close()

    # Process legacy snapshot
    out_path = tmp_path / "indexed_legacy.sqlite"
    process_database(db_path, out_path)

    # Verify indexed legacy database
    conn = sqlite3.connect(out_path)
    verify_database(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT normalized_name FROM projects_search")
    assert cursor.fetchone()[0] == "legacy-pkg"
    conn.close()

    # Validate with runtime validator
    validate_db(out_path)


def test_empty_fts_index_caught_by_integrity_check(tmp_path):
    """
    Test that an empty FTS index (where shadow tables were not populated)
    is caught by the content-aware FTS5 integrity check during generation,
    and by the MATCH probe at runtime.
    """
    db_path = tmp_path / "empty_fts.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE projects (
            name TEXT NOT NULL,
            version TEXT,
            summary TEXT,
            upload_time TEXT
        )
    """)
    conn.execute(
        "INSERT INTO projects VALUES ('requests', '2.31.0', 'HTTP for humans', '2023-01-01')"
    )
    conn.execute("""
        CREATE TABLE projects_search (
            project_rowid INTEGER PRIMARY KEY,
            normalized_name TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("INSERT INTO projects_search VALUES (1, 'requests')")

    # Create FTS5 table without rebuilding it
    conn.execute("""
        CREATE VIRTUAL TABLE projects_fts USING fts5(
            normalized_name,
            content='projects_search',
            content_rowid='project_rowid',
            tokenize='trigram'
        )
    """)
    conn.commit()

    # Content-aware integrity check must fail because FTS index is empty while content table has rows
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(
            "INSERT INTO projects_fts(projects_fts, rank) VALUES('integrity-check', 1)"
        )
    conn.close()

    # Runtime validate_db must fail due to MATCH probe
    with pytest.raises(RuntimeError, match="FTS5 index probe failed"):
        validate_db(db_path)


def test_atomic_replacement_preserves_valid_db_on_failure(tmp_path):
    """
    Test that process_database preserves the existing valid database
    if rebuilding/validation encounters a failure.
    """
    valid_db = tmp_path / "target.sqlite"
    create_fixture_database(valid_db)
    process_database(valid_db, valid_db)

    # Read original file content
    orig_content = valid_db.read_bytes()

    # Create an invalid source database (missing required columns)
    invalid_source = tmp_path / "invalid.sqlite"
    conn = sqlite3.connect(invalid_source)
    conn.execute("CREATE TABLE projects (invalid_col TEXT)")
    conn.execute("INSERT INTO projects VALUES ('data')")
    conn.commit()
    conn.close()

    # Attempt to process invalid source into valid_db
    with pytest.raises(ValueError, match="missing required columns"):
        process_database(invalid_source, valid_db)

    # Verify valid_db was preserved and untouched
    assert valid_db.read_bytes() == orig_content


def test_metadata_generation_and_checksum_verification(tmp_path):
    """Test snapshot metadata generation and checksum verification."""
    db_path = tmp_path / "check.sqlite"
    meta_path = tmp_path / "check.sqlite.meta.json"
    create_fixture_database(db_path)

    process_database(db_path, db_path, meta_path)

    assert meta_path.is_file()
    with open(meta_path) as f:
        meta = json.load(f)

    assert meta["row_count"] == 20
    assert meta["search_schema_version"] == "1.0.0"
    assert "sha256" in meta
    assert "sqlite_version" in meta

    # Checksum passes
    assert verify_snapshot_checksum(db_path, meta_path) is True

    # Legacy snapshot without metadata returns False without error
    missing_meta = tmp_path / "nonexistent.meta.json"
    assert verify_snapshot_checksum(db_path, missing_meta) is False

    # Tampered database causes checksum verification to raise ValueError
    with open(db_path, "ab") as f:
        f.write(b"corruption")
    with pytest.raises(ValueError, match="Snapshot checksum mismatch"):
        verify_snapshot_checksum(db_path, meta_path)
