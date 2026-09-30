#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "click>=8.1.8",
# ]
# ///
"""
Snapshot bundling script for Docker builds and server packaging.
Copies a specified source SQLite snapshot and its metadata into the server context
after verifying integrity and checksums, preventing accidental packaging of stale
or fixture data.
"""

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import click

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root))

from build_search_index import REQUIRED_COLUMNS
from sqlite_runtime import verify_sqlite_runtime

# Default source SQLite database to bundle
DEFAULT_SOURCE_DB = "pypi_data.sqlite"

# Default destination directory for server build context
DEFAULT_DEST_DIR = "server"


def compute_file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_snapshot_integrity(db_path: Path, meta_path: Path) -> dict:
    """Validate snapshot existence, checksum against metadata, and SQLite schema integrity."""
    if not db_path.is_file():
        raise FileNotFoundError(f"Snapshot database not found at {db_path}")
    if not meta_path.is_file():
        raise FileNotFoundError(f"Snapshot metadata not found at {meta_path}")

    with open(meta_path, encoding="utf-8") as f:
        try:
            meta = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt metadata JSON at {meta_path}: {e}") from e

    expected_sha = meta.get("sha256")
    if not expected_sha:
        raise ValueError(f"Metadata at {meta_path} missing 'sha256' property")

    actual_sha = compute_file_sha256(db_path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"Snapshot checksum mismatch for {db_path}:\n"
            f"  Expected (meta): {expected_sha}\n"
            f"  Actual   (file): {actual_sha}"
        )

    # SQLite integrity check
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check(1)")
        res = cur.fetchone()
        if not res or res[0] != "ok":
            raise RuntimeError(f"Database integrity check failed: {res}")

        cur.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('projects', 'projects_search', 'projects_fts')"
        )
        table_count = cur.fetchone()[0]
        if table_count < 3:
            raise RuntimeError(
                f"Database missing required search tables (found {table_count}/3)"
            )

        cur.execute("PRAGMA table_info(projects)")
        cols = {r[1] for r in cur.fetchall()}
        missing = REQUIRED_COLUMNS - cols
        if missing:
            raise RuntimeError(f"Projects table missing required columns: {missing}")
    finally:
        conn.close()

    return meta


def bundle_snapshot(
    source_db: Path,
    source_meta: Path | None = None,
    dest_dir: Path | None = None,
) -> dict:
    verify_sqlite_runtime()

    resolved_source_db = source_db.resolve()
    if not resolved_source_db.is_file():
        # Check if compressed archive exists
        gz_candidate = resolved_source_db.with_name(resolved_source_db.name + ".gz")
        if gz_candidate.is_file():
            print(f"Decompressing {gz_candidate} -> {resolved_source_db}...")
            with (
                gzip.open(gz_candidate, "rb") as f_in,
                open(resolved_source_db, "wb") as f_out,
            ):
                shutil.copyfileobj(f_in, f_out)
        else:
            raise FileNotFoundError(
                f"Source snapshot database not found at {resolved_source_db}.\n"
                f"Please build or generate a snapshot first (e.g. 'just build_sqlite' or 'just fixture_db')."
            )

    if source_meta:
        resolved_source_meta = source_meta.resolve()
    else:
        # Default metadata path: <db_name>.meta.json or <db_name_without_ext>.sqlite.meta.json
        resolved_source_meta = resolved_source_db.with_name(
            resolved_source_db.name + ".meta.json"
        )
        if not resolved_source_meta.is_file():
            resolved_source_meta = resolved_source_db.with_suffix(".sqlite.meta.json")

    if not resolved_source_meta.is_file():
        raise FileNotFoundError(
            f"Source snapshot metadata not found at {resolved_source_meta}.\n"
            f"A valid metadata file is required to verify snapshot integrity before bundling."
        )

    print(
        f"Validating source snapshot at {resolved_source_db} against {resolved_source_meta}..."
    )
    validate_snapshot_integrity(resolved_source_db, resolved_source_meta)

    target_dir = (dest_dir or (repo_root / "server")).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_db = target_dir / "pypi_data.sqlite"
    target_meta = target_dir / "pypi_data.sqlite.meta.json"

    # Don't copy if source and destination are the exact same path
    if resolved_source_db != target_db:
        print(f"Bundling snapshot into server context: {target_db}...")
        # Staged atomic copy
        tmp_db = target_dir / f".tmp_bundle_{os.getpid()}.sqlite"
        tmp_meta = target_dir / f".tmp_bundle_{os.getpid()}.sqlite.meta.json"
        try:
            shutil.copyfile(resolved_source_db, tmp_db)
            shutil.copyfile(resolved_source_meta, tmp_meta)
            os.replace(tmp_db, target_db)
            os.replace(tmp_meta, target_meta)
        finally:
            if tmp_db.exists():
                tmp_db.unlink()
            if tmp_meta.exists():
                tmp_meta.unlink()

    # Re-validate destination snapshot
    dest_meta = validate_snapshot_integrity(target_db, target_meta)

    print("Snapshot successfully bundled:")
    print(f" - SQLite:   {target_db} ({target_db.stat().st_size} bytes)")
    print(f" - Metadata: {target_meta}")
    print(f" - Rows:     {dest_meta.get('row_count')}")
    print(f" - SHA256:   {dest_meta.get('sha256')}")

    return dest_meta


@click.command()
@click.option(
    "--source-db",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to source SQLite database (defaults to root pypi_data.sqlite).",
)
@click.option(
    "--source-meta",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to source metadata JSON (defaults to <source-db>.meta.json).",
)
@click.option(
    "--dest-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Destination directory (defaults to server/).",
)
def cli(source_db: Path | None, source_meta: Path | None, dest_dir: Path | None):
    """Bundle and validate snapshot database into server build context."""
    actual_source_db = source_db or (repo_root / DEFAULT_SOURCE_DB)
    actual_dest_dir = dest_dir or (repo_root / DEFAULT_DEST_DIR)

    try:
        bundle_snapshot(
            source_db=actual_source_db,
            source_meta=source_meta,
            dest_dir=actual_dest_dir,
        )
    except Exception as e:
        click.echo(f"Snapshot bundling failed: {e}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
