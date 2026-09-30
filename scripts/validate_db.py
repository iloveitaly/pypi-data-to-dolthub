#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "click>=8.1.8",
# ]
# ///
"""
Shared database validation script.
Used across CI, publishing workflows, and local development to verify
snapshot integrity, schema correctness, FTS index health, and optional checksums.
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import click

# Add project root and server directory to path
repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root))

from sqlite_runtime import verify_sqlite_runtime

# Mandatory columns required on the projects table for search indexing
REQUIRED_COLUMNS = {"name", "version", "summary", "upload_time"}


def escape_fts5_token(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def compute_sha256(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_database(db_path: Path, meta_path: Path | None = None) -> None:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    # Verify SQLite runtime capability
    sqlite_ver = verify_sqlite_runtime()
    print(f"SQLite runtime verified: {sqlite_ver}")

    # Checksum verification if metadata is supplied or exists alongside database
    if meta_path and meta_path.is_file():
        print(f"Verifying checksum against {meta_path}...")
        with open(meta_path) as f:
            meta = json.load(f)
        expected_sha = meta.get("sha256")
        if expected_sha:
            actual_sha = compute_sha256(db_path)
            if actual_sha.lower() != expected_sha.lower():
                raise ValueError(
                    f"Checksum mismatch: expected {expected_sha}, got {actual_sha}"
                )
            print("Checksum verified successfully.")

    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    try:
        cursor = conn.cursor()

        # 1. PRAGMA integrity_check
        cursor.execute("PRAGMA integrity_check(1)")
        res = cursor.fetchone()
        if not res or res[0] != "ok":
            raise RuntimeError(f"Database integrity check failed: {res}")

        # 2. Required projects table & columns
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects'"
        )
        if not cursor.fetchone():
            raise RuntimeError("Database missing required 'projects' table")

        cursor.execute("PRAGMA table_info(projects)")
        columns = {r[1] for r in cursor.fetchall()}
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise RuntimeError(f"Projects table missing required columns: {missing}")

        cursor.execute("SELECT COUNT(*) FROM projects")
        count = cursor.fetchone()[0]
        if count == 0:
            raise RuntimeError("Projects table is empty")
        print(f"Projects table verified: {count} packages present.")

        # 3. Search tables
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects_search'"
        )
        if not cursor.fetchone():
            raise RuntimeError("Database missing required 'projects_search' table")

        cursor.execute("SELECT COUNT(*) FROM projects_search")
        search_count = cursor.fetchone()[0]
        if search_count == 0:
            raise RuntimeError("projects_search table is empty")

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects_fts'"
        )
        if not cursor.fetchone():
            raise RuntimeError("Database missing required 'projects_fts' virtual table")

        # 4. FTS read probe using MATCH against an indexed name
        cursor.execute(
            "SELECT normalized_name FROM projects_search WHERE length(normalized_name) >= 3 LIMIT 1"
        )
        sample_row = cursor.fetchone()
        if sample_row:
            probe_token = escape_fts5_token(sample_row[0][:3])
            cursor.execute(
                "SELECT 1 FROM projects_fts WHERE projects_fts MATCH ? LIMIT 1",
                (probe_token,),
            )
            if not cursor.fetchone():
                raise RuntimeError(
                    "Database FTS5 index probe failed: index empty or desynchronized"
                )
        print("Search and FTS5 trigram indexes verified.")

    finally:
        conn.close()


@click.command()
@click.argument("db_path", type=click.Path(path_type=Path))
@click.option(
    "--meta",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to metadata JSON file for checksum verification.",
)
def cli(db_path: Path, meta: Path | None):
    """Validate SQLite PyPI database snapshot."""
    resolved_db = db_path.resolve()
    resolved_meta = meta.resolve() if meta else None

    try:
        validate_database(resolved_db, resolved_meta)
        click.echo(f"All validations passed for {resolved_db}")
    except Exception as e:
        click.echo(f"Validation ERROR: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
