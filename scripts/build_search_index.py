#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "click>=8.1.8",
# ]
# ///
"""
Build search indexes and validate SQLite snapshots for PyPI metadata.
Creates normalized-name lookup table, B-tree index, and FTS5 trigram index.
Generates snapshot metadata and checksum.
"""

import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import click

# Add server directory and repo root to sys.path for sqlite_runtime import
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root))
try:
    from sqlite_runtime import (
        PINNED_SQLITE_VERSION,
        SEARCH_SCHEMA_VERSION,
        verify_sqlite_runtime,
    )
except ImportError:
    try:
        from server.sqlite_runtime import (
            PINNED_SQLITE_VERSION,
            SEARCH_SCHEMA_VERSION,
            verify_sqlite_runtime,
        )
    except ImportError:
        PINNED_SQLITE_VERSION = "3.53.1"
        SEARCH_SCHEMA_VERSION = "1.0.0"

        def verify_sqlite_runtime(conn=None):
            return sqlite3.sqlite_version


# Default database filename for search index processing
DEFAULT_DB_PATH = "pypi_data.sqlite"

# Mandatory columns required on the projects table for search indexing
REQUIRED_COLUMNS = {"name", "version", "summary", "upload_time"}


def normalize_name(name: str) -> str:
    """
    PEP 503 name normalization:
    lowercase, collapsing runs of '-', '_', and '.' to '-'.
    """
    if not name:
        return ""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def compute_sha256(filepath: str | Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_source_projects(conn: sqlite3.Connection) -> int:
    """Validate that the source projects table exists, has required columns, and is non-empty."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
    )
    if not cursor.fetchone():
        raise ValueError("Database missing required 'projects' table.")

    cursor.execute("PRAGMA table_info(projects)")
    columns = {row[1] for row in cursor.fetchall()}
    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise ValueError(f"Projects table missing required columns: {missing}")

    cursor.execute("SELECT COUNT(*) FROM projects")
    count = cursor.fetchone()[0]
    if count == 0:
        raise ValueError("Projects table is empty.")

    return count


def build_indexes(conn: sqlite3.Connection):
    """Create normalized search table, B-tree index, and FTS5 trigram virtual table."""
    if conn.in_transaction:
        conn.commit()
    cursor = conn.cursor()

    # Bulk indexing performance pragmas
    try:
        cursor.execute("PRAGMA synchronous = OFF")
        cursor.execute("PRAGMA journal_mode = MEMORY")
        cursor.execute("PRAGMA cache_size = -1000000")  # ~1GB cache
        cursor.execute("PRAGMA temp_store = MEMORY")
    except sqlite3.OperationalError:
        pass

    # Drop existing search tables if present
    cursor.execute("DROP TABLE IF EXISTS projects_fts")
    cursor.execute("DROP TABLE IF EXISTS projects_search")

    # Create normalized name table
    cursor.execute("""
        CREATE TABLE projects_search (
            project_rowid INTEGER PRIMARY KEY,
            normalized_name TEXT NOT NULL UNIQUE
        )
    """)

    # Populate normalized names in Python for maximum throughput
    print("Reading and normalizing project names...")
    cursor.execute("SELECT rowid, name, upload_time FROM projects ORDER BY rowid ASC")
    rows = cursor.fetchall()

    norm_map: dict[str, tuple[int, str]] = {}
    for rid, name, upload_time in rows:
        if name:
            norm = normalize_name(name)
            if norm:
                up_time = upload_time or ""
                # In case of normalized collision, keep latest upload_time
                if norm not in norm_map or up_time > norm_map[norm][1]:
                    norm_map[norm] = (rid, up_time)

    records = [(rid, norm) for norm, (rid, _) in norm_map.items()]
    print(f"Inserting {len(records)} normalized search records...")
    cursor.executemany(
        "INSERT INTO projects_search (project_rowid, normalized_name) VALUES (?, ?)",
        records,
    )

    # Create index on projects(name) for fast exact name lookups
    print("Building indexes...")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_name ON projects(name)")
    # Note: projects_search.normalized_name is already indexed via its UNIQUE constraint.

    # Create FTS5 trigram virtual table with external content
    print("Building FTS5 trigram index...")
    cursor.execute("""
        CREATE VIRTUAL TABLE projects_fts USING fts5(
            normalized_name,
            content='projects_search',
            content_rowid='project_rowid',
            tokenize='trigram'
        )
    """)

    # Populate and optimize FTS index
    cursor.execute("INSERT INTO projects_fts(projects_fts) VALUES('rebuild')")
    cursor.execute("INSERT INTO projects_fts(projects_fts) VALUES('optimize')")

    # Collect query planner statistics
    print("Collecting planner statistics...")
    cursor.execute("ANALYZE")
    conn.commit()


def verify_database(conn: sqlite3.Connection):
    """Run integrity and consistency checks on the database."""
    cursor = conn.cursor()

    # PRAGMA integrity_check
    cursor.execute("PRAGMA integrity_check")
    integrity = cursor.fetchall()
    if not integrity or integrity[0][0] != "ok":
        raise ValueError(f"SQLite integrity check failed: {integrity}")

    # FTS5 content-aware integrity-check (rank=1 validates external content consistency)
    cursor.execute(
        "INSERT INTO projects_fts(projects_fts, rank) VALUES('integrity-check', 1)"
    )

    # Verify search table count
    cursor.execute("SELECT COUNT(*) FROM projects_search")
    search_count = cursor.fetchone()[0]
    if search_count == 0:
        raise ValueError("projects_search table is empty after indexing.")

    # Verify normalized_name uniqueness
    cursor.execute(
        "SELECT COUNT(DISTINCT normalized_name), COUNT(*) FROM projects_search"
    )
    distinct_count, total_count = cursor.fetchone()
    if distinct_count != total_count:
        raise ValueError(
            f"Normalized names not unique: {distinct_count} distinct vs {total_count} total"
        )

    conn.commit()


def create_fixture_database(db_path: str | Path):
    """Generate a realistic test fixture database with varied packages for testing."""
    path = Path(db_path)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(path)
    cursor = conn.cursor()
    cursor.execute("""
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

    fixtures = [
        ("requests", "2.31.0", "Python HTTP for Humans.", "2023-05-22T00:00:00Z"),
        (
            "requests-mock",
            "1.12.1",
            "Mock out responses from the requests package",
            "2024-03-01T00:00:00Z",
        ),
        (
            "pytest",
            "8.1.1",
            "pytest: simple powerful testing with Python",
            "2024-03-10T00:00:00Z",
        ),
        (
            "pytest-mock",
            "3.14.0",
            "Thin-wrapper around the mock package for easier use with pytest",
            "2024-03-22T00:00:00Z",
        ),
        (
            "urllib3",
            "2.2.1",
            "HTTP library with thread-safe connection pooling, file post, and more.",
            "2024-02-16T00:00:00Z",
        ),
        (
            "fastapi",
            "0.110.0",
            "FastAPI framework, high performance, easy to learn, fast to code, ready for production",
            "2024-03-04T00:00:00Z",
        ),
        (
            "uvicorn",
            "0.28.0",
            "The lightning-fast ASGI server.",
            "2024-03-09T00:00:00Z",
        ),
        (
            "flask",
            "3.0.2",
            "A simple framework for building complex web applications.",
            "2024-02-05T00:00:00Z",
        ),
        (
            "django",
            "5.0.3",
            "A high-level Python web framework that encourages rapid development.",
            "2024-03-04T00:00:00Z",
        ),
        (
            "pydantic",
            "2.6.4",
            "Data validation using Python type hints",
            "2024-03-13T00:00:00Z",
        ),
        (
            "pydantic-core",
            "2.16.3",
            "Core functionality for Pydantic validation and serialization",
            "2024-02-28T00:00:00Z",
        ),
        (
            "numpy",
            "1.26.4",
            "Fundamental package for array computing in Python",
            "2024-02-06T00:00:00Z",
        ),
        (
            "pandas",
            "2.2.1",
            "Powerful data structures for data analysis, time series, and statistics",
            "2024-02-23T00:00:00Z",
        ),
        (
            "scipy",
            "1.12.0",
            "Fundamental algorithms for scientific computing in Python",
            "2024-01-20T00:00:00Z",
        ),
        ("a", "1.0.0", "Single letter package A", "2020-01-01T00:00:00Z"),
        ("ab", "1.0.0", "Two letter package AB", "2020-01-02T00:00:00Z"),
        ("abc-test", "0.1.0", "ABC test package", "2021-01-01T00:00:00Z"),
        (
            "tool_123",
            "0.0.1",
            "Digit heavy tool with underscore",
            "2022-05-01T00:00:00Z",
        ),
        ("123-tool", "0.0.1", "Leading digit tool", "2022-05-02T00:00:00Z"),
        (
            "Foo.Bar_Baz",
            "1.0.0",
            "Mixed case and multiple separators",
            "2023-01-01T00:00:00Z",
        ),
    ]

    for f in fixtures:
        cursor.execute(
            """
            INSERT INTO projects (
                name, version, summary, upload_time, package_url, project_url
            ) VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                f[0],
                f[1],
                f[2],
                f[3],
                f"https://pypi.org/project/{f[0]}/",
                f"https://pypi.org/project/{f[0]}/",
            ),
        )

    conn.commit()
    conn.close()


def verify_snapshot_checksum(db_path: str | Path, meta_path: str | Path) -> bool:
    """
    Verify the SHA-256 checksum of db_path against meta_path if metadata exists.
    Returns True if checksum matched. Returns False if meta_path does not exist (legacy snapshot).
    Raises ValueError if meta_path exists but checksum does not match.
    """
    meta_p = Path(meta_path).resolve()
    if not meta_p.is_file():
        return False

    with open(meta_p) as f:
        meta = json.load(f)

    expected = meta.get("sha256")
    if not expected:
        return False

    actual = compute_sha256(db_path)
    if actual.lower() != expected.lower():
        raise ValueError(
            f"Snapshot checksum mismatch for {db_path}: expected {expected}, got {actual}"
        )
    return True


def process_database(
    input_db: str | Path, output_db: str | Path, meta_path: str | Path | None = None
):
    """
    Reads input_db, builds indexes in a temporary copy, validates,
    and atomically replaces output_db. Writes snapshot metadata.
    """
    input_path = Path(input_db).resolve()
    output_path = Path(output_db).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input database not found: {input_path}")

    # Verify SQLite runtime capability before processing
    active_sqlite_ver = verify_sqlite_runtime()

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Work in a temporary file in the same directory for atomic rename
    with tempfile.NamedTemporaryFile(
        dir=output_path.parent, delete=False, suffix=".sqlite.tmp"
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        shutil.copyfile(input_path, tmp_path)

        conn = sqlite3.connect(tmp_path)
        try:
            row_count = validate_source_projects(conn)
            print(
                f"Validated source projects table ({row_count} rows). Building indexes..."
            )
            build_indexes(conn)
            print("Verifying database integrity and index consistency...")
            verify_database(conn)
        finally:
            conn.close()

        # Atomic replacement: only called if indexing and verification succeed
        tmp_path.replace(output_path)
        print(f"Successfully wrote indexed database to {output_path}")

        # Write snapshot metadata
        sha256 = compute_sha256(output_path)
        meta = {
            "snapshot_timestamp": datetime.now(UTC).isoformat(),
            "row_count": row_count,
            "sha256": sha256,
            "search_schema_version": SEARCH_SCHEMA_VERSION,
            "sqlite_version": active_sqlite_ver,
        }

        target_meta_path = (
            Path(meta_path).resolve()
            if meta_path
            else output_path.with_suffix(".sqlite.meta.json")
        )
        target_meta_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_meta_path, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"Wrote snapshot metadata to {target_meta_path}")

    finally:
        if tmp_path.exists():
            tmp_path.unlink()


@click.command()
@click.argument("input_db", required=False, default=None)
@click.argument("output_db", required=False, default=None)
@click.option(
    "--meta",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to write metadata JSON (defaults to <output>.meta.json).",
)
@click.option(
    "--create-fixture",
    type=click.Path(path_type=Path),
    default=None,
    help="Create a rich fixture database at the given path and index it.",
)
@click.option(
    "--verify-checksum",
    type=click.Path(path_type=Path),
    default=None,
    help="Verify checksum of input_db against specified metadata JSON.",
)
def cli(
    input_db: str | None,
    output_db: str | None,
    meta: Path | None,
    create_fixture: Path | None,
    verify_checksum: Path | None,
):
    """Build PyPI SQLite search indexes and metadata."""
    if verify_checksum:
        db_to_verify = input_db or DEFAULT_DB_PATH
        matched = verify_snapshot_checksum(db_to_verify, verify_checksum)
        if matched:
            click.echo(f"Snapshot checksum verified successfully for {db_to_verify}")
        else:
            click.echo(f"No metadata file found at {verify_checksum} (legacy snapshot)")
        return

    if create_fixture:
        click.echo(f"Generating fixture database at {create_fixture}...")
        create_fixture_database(create_fixture)
        process_database(create_fixture, create_fixture, meta)
        return

    target_input_db = input_db or DEFAULT_DB_PATH
    target_output_db = output_db or target_input_db
    process_database(target_input_db, target_output_db, meta)


if __name__ == "__main__":
    cli()
