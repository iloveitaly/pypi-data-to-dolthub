#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "click>=8.1.8",
# ]
# ///
"""
Snapshot preparation script for local use and GitHub Actions.
Supports two converged paths:
1. Full BigQuery data refresh (--refresh).
2. Download existing latest GitHub release, verify checksum (if metadata exists),
   and rebuild search indexes using current code so legacy snapshots and schema
   updates work without re-querying BigQuery.
Outputs a single compressed snapshot (pypi_data.sqlite.gz) plus metadata.
"""

import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import click

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root))

from build_search_index import (
    process_database,
    verify_snapshot_checksum,
)
from sqlite_runtime import verify_sqlite_runtime

# Default GitHub repository hosting published releases
DEFAULT_GITHUB_REPOSITORY = "iloveitaly/pypi-data-to-dolthub"

# Standard HTTP user agent for downloading release artifacts
HTTP_USER_AGENT = "PyPI-PrepareSnapshot/1.0"


def download_file(url: str, dest: Path) -> bool:
    """Download a file via HTTP GET. Returns True on success, False if 404/not found."""
    print(f"Downloading {url} -> {dest}...")
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    try:
        with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:
            shutil.copyfileobj(resp, out)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def decompress_gzip(src: Path, dest: Path) -> None:
    print(f"Decompressing {src} -> {dest}...")
    with gzip.open(src, "rb") as f_in, open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def compress_gzip(src: Path, dest: Path) -> None:
    print(f"Compressing {src} -> {dest}...")
    with open(src, "rb") as f_in, gzip.open(dest, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)


def verify_staged_snapshot(staged_db: Path, staged_meta: Path, staged_gz: Path) -> dict:
    """Verify staged snapshot integrity and gzip decompression checksum before publishing."""
    if not staged_db.is_file() or staged_db.stat().st_size == 0:
        raise RuntimeError(f"Staged database is missing or empty: {staged_db}")
    if not staged_meta.is_file() or staged_meta.stat().st_size == 0:
        raise RuntimeError(f"Staged metadata is missing or empty: {staged_meta}")
    if not staged_gz.is_file() or staged_gz.stat().st_size == 0:
        raise RuntimeError(f"Staged gzip archive is missing or empty: {staged_gz}")

    with open(staged_meta, encoding="utf-8") as f:
        meta = json.load(f)

    expected_sha = meta.get("sha256")
    if not expected_sha:
        raise RuntimeError("Staged metadata missing required sha256 field")

    hasher = hashlib.sha256()
    decompressed_bytes = 0
    with gzip.open(staged_gz, "rb") as gz_in:
        while chunk := gz_in.read(1024 * 1024):
            hasher.update(chunk)
            decompressed_bytes += len(chunk)

    computed_sha = hasher.hexdigest()
    if computed_sha != expected_sha:
        raise RuntimeError(
            f"Staged gzip decompressed checksum mismatch: expected {expected_sha}, got {computed_sha}"
        )
    if decompressed_bytes != staged_db.stat().st_size:
        raise RuntimeError(
            f"Staged gzip decompressed size ({decompressed_bytes}) != staged db size ({staged_db.stat().st_size})"
        )

    return meta


def atomic_replace_snapshot_files(replacements: list[tuple[Path, Path]]) -> None:
    """
    Replace multiple destination files with staged files atomically.
    Backs up existing targets to a rollback directory on the same filesystem
    and restores them if any replacement fails.
    replacements is a list of (staged_path, final_path).
    """
    if not replacements:
        return

    dest_dir = replacements[0][1].parent
    rollback_dir = dest_dir / f".rollback_{os.getpid()}_{id(replacements)}"
    rollback_dir.mkdir(parents=True, exist_ok=True)

    backed_up: list[tuple[Path, Path]] = []
    replaced: list[Path] = []

    try:
        # Step 1: Backup any existing target files
        for _staged_path, final_path in replacements:
            if final_path.exists():
                backup_path = rollback_dir / final_path.name
                shutil.copy2(final_path, backup_path)
                backed_up.append((final_path, backup_path))

        # Step 2: Atomic replace each file
        for staged_path, final_path in replacements:
            os.replace(staged_path, final_path)
            replaced.append(final_path)

    except Exception as e:
        # Step 3: Rollback on any failure
        print(
            f"Error during snapshot file replacement: {e}. Rolling back...",
            file=sys.stderr,
        )
        for final_path in replaced:
            try:
                if final_path.exists():
                    final_path.unlink()
            except Exception:
                pass
        for final_path, backup_path in backed_up:
            try:
                if backup_path.exists():
                    os.replace(backup_path, final_path)
            except Exception:
                pass
        raise
    finally:
        # Step 4: Clean up rollback directory
        shutil.rmtree(rollback_dir, ignore_errors=True)


def prepare_snapshot(
    refresh: bool = False,
    output_dir: Path | None = None,
    repo: str = "iloveitaly/pypi-data-to-dolthub",
) -> dict:
    verify_sqlite_runtime()
    out_dir = (output_dir or repo_root).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    final_db = out_dir / "pypi_data.sqlite"
    final_gz = out_dir / "pypi_data.sqlite.gz"
    final_meta = out_dir / "pypi_data.sqlite.meta.json"

    with tempfile.TemporaryDirectory(
        dir=out_dir, prefix="snapshot_prep_"
    ) as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        raw_db = tmp_dir / "raw.sqlite"

        if refresh:
            print("=== Full Data Refresh Mode (BigQuery) ===")
            # 1. Fetch BigQuery parquet export
            print("Executing fetch_pypi_data.py...")
            subprocess.run(
                [str(repo_root / "scripts" / "fetch_pypi_data.py")],
                check=True,
                cwd=repo_root,
            )

            # 2. Run DuckDB aggregation into temporary raw SQLite
            print("Executing DuckDB SQL aggregation...")
            duckdb_temp = tmp_dir / "duckdb_temp"
            duckdb_temp.mkdir(parents=True, exist_ok=True)
            sql_script = repo_root / "scripts" / "sql" / "build_latest_sqlite.sql"

            sql_content = sql_script.read_text()
            sql_content = re.sub(
                r"SET\s+temp_directory\s*=\s*'[^']*';",
                f"SET temp_directory = '{duckdb_temp.as_posix()}';",
                sql_content,
            )
            sql_content = sql_content.replace(
                "ATTACH 'pypi_data.sqlite' AS sqlite_db (TYPE sqlite);",
                f"ATTACH '{raw_db.as_posix()}' AS sqlite_db (TYPE sqlite);",
            )

            try:
                subprocess.run(
                    ["duckdb"],
                    input=sql_content.encode("utf-8"),
                    check=True,
                    cwd=repo_root,
                )
            finally:
                shutil.rmtree(duckdb_temp, ignore_errors=True)

            # Clean up parquet artifact
            parquet_file = repo_root / "pypi_metadata.parquet"
            if parquet_file.exists():
                parquet_file.unlink()

        else:
            print("=== Download Existing Release Snapshot Mode ===")
            release_base = f"https://github.com/{repo}/releases/download/latest"
            db_gz_url = f"{release_base}/pypi_data.sqlite.gz"
            meta_url = f"{release_base}/pypi_data.sqlite.meta.json"

            download_gz = tmp_dir / "downloaded.sqlite.gz"
            download_meta = tmp_dir / "downloaded.sqlite.meta.json"

            if not download_file(db_gz_url, download_gz):
                raise RuntimeError(
                    f"Failed to download release snapshot from {db_gz_url}"
                )

            has_meta = download_file(meta_url, download_meta)
            decompress_gzip(download_gz, raw_db)

            if has_meta:
                print("Verifying downloaded snapshot checksum...")
                verify_snapshot_checksum(raw_db, download_meta)
                print("Downloaded snapshot checksum verified successfully.")
            else:
                print(
                    "No metadata found for downloaded release (supporting initial legacy release)."
                )

        # 3. Converged Index Building & Validation
        print("Building search indexes and validating snapshot...")
        processed_db = tmp_dir / "pypi_data.indexed.sqlite"
        processed_meta = tmp_dir / "pypi_data.indexed.meta.json"
        process_database(raw_db, processed_db, processed_meta)

        # 4. Stage gzip compression in tmp_dir before touching final paths
        staged_gz = tmp_dir / "pypi_data.indexed.sqlite.gz"
        compress_gzip(processed_db, staged_gz)

        # 5. Verify staged artifacts before replacing published files
        print("Verifying staged snapshot artifacts...")
        metadata = verify_staged_snapshot(processed_db, processed_meta, staged_gz)
        print("Staged artifacts verified successfully.")

        # 6. Atomic replacement of destination files with rollback
        print(f"Atomically publishing validated snapshot to {out_dir}...")
        replacements = [
            (processed_db, final_db),
            (processed_meta, final_meta),
            (staged_gz, final_gz),
        ]
        atomic_replace_snapshot_files(replacements)

        print("Snapshot prepared successfully:")
        print(f" - SQLite:   {final_db} ({final_db.stat().st_size} bytes)")
        print(f" - Gzip:     {final_gz} ({final_gz.stat().st_size} bytes)")
        print(f" - Metadata: {final_meta}")
        print(f" - Rows:     {metadata.get('row_count')}")
        print(f" - SHA256:   {metadata.get('sha256')}")

        return metadata


@click.command()
@click.option(
    "--refresh",
    is_flag=True,
    help="Perform full BigQuery refresh.",
)
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Output directory for snapshot and metadata.",
)
@click.option(
    "--repo",
    default=os.getenv("GITHUB_REPOSITORY", DEFAULT_GITHUB_REPOSITORY),
    help="GitHub repository name for release download.",
)
def cli(refresh: bool, output_dir: Path | None, repo: str):
    """Prepare PyPI SQLite snapshot with indexes."""
    metadata = prepare_snapshot(refresh=refresh, output_dir=output_dir, repo=repo)

    gha_output = os.getenv("GITHUB_OUTPUT")
    if gha_output:
        with open(gha_output, "a") as f:
            f.write(f"refreshed={'true' if refresh else 'false'}\n")
            f.write(f"row_count={metadata.get('row_count', 0)}\n")
            f.write(f"sha256={metadata.get('sha256', '')}\n")

    gha_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if gha_summary:
        with open(gha_summary, "a") as f:
            f.write("### Prepared Snapshot Summary\n")
            f.write(f"- **Refreshed:** `{'true' if refresh else 'false'}`\n")
            f.write(f"- **Row Count:** `{metadata.get('row_count')}`\n")
            f.write(f"- **SHA-256:** `{metadata.get('sha256')}`\n")
            f.write(f"- **SQLite Version:** `{metadata.get('sqlite_version')}`\n")


if __name__ == "__main__":
    cli()
