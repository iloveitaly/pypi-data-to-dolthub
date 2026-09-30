"""Tests for snapshot preparation, failure injection, and atomic publication with rollback."""

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add repo root and server to sys.path
repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "scripts"))

from build_search_index import (
    process_database,
)
from prepare_snapshot import (
    compress_gzip,
    prepare_snapshot,
)


def create_fixture_db(path: Path, package_names: list[str]) -> None:
    """Create a minimal valid projects database for snapshot testing."""
    conn = sqlite3.connect(path)
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
    for name in package_names:
        cur.execute(
            "INSERT INTO projects (name, version, summary, upload_time) VALUES (?, '1.0.0', 'Test', '2026-01-01T00:00:00Z')",
            (name,),
        )
    conn.commit()
    conn.close()


def make_snapshot_bundle(
    dest_dir: Path, package_names: list[str], prefix: str = "pypi_data"
) -> dict:
    """Create a complete set of .sqlite, .sqlite.meta.json, and .sqlite.gz in dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw_path = dest_dir / f"{prefix}.raw.sqlite"
    db_path = dest_dir / f"{prefix}.sqlite"
    meta_path = dest_dir / f"{prefix}.sqlite.meta.json"
    gz_path = dest_dir / f"{prefix}.sqlite.gz"

    create_fixture_db(raw_path, package_names)
    process_database(raw_path, db_path, meta_path)
    if raw_path.exists():
        raw_path.unlink()
    compress_gzip(db_path, gz_path)
    with open(meta_path) as f:
        return json.load(f)


def test_prepare_snapshot_success(tmp_path):
    """Test successful snapshot preparation with mocked download and verify decompression checksum."""
    src_dir = tmp_path / "source"
    out_dir = tmp_path / "output"

    _src_meta = make_snapshot_bundle(src_dir, ["pkg-one", "pkg-two"], prefix="release")
    src_gz = src_dir / "release.sqlite.gz"
    src_meta_file = src_dir / "release.sqlite.meta.json"

    def mock_download_file(url: str, dest: Path) -> bool:
        if url.endswith(".sqlite.gz"):
            shutil.copyfile(src_gz, dest)
            return True
        elif url.endswith(".meta.json"):
            shutil.copyfile(src_meta_file, dest)
            return True
        return False

    with patch("prepare_snapshot.download_file", side_effect=mock_download_file):
        meta = prepare_snapshot(refresh=False, output_dir=out_dir)

    final_db = out_dir / "pypi_data.sqlite"
    final_meta = out_dir / "pypi_data.sqlite.meta.json"
    final_gz = out_dir / "pypi_data.sqlite.gz"

    assert final_db.is_file()
    assert final_meta.is_file()
    assert final_gz.is_file()

    # Verify decompressed content matches published db and metadata sha256
    with gzip.open(final_gz, "rb") as gz_in:
        decompressed_data = gz_in.read()

    assert hashlib.sha256(decompressed_data).hexdigest() == meta["sha256"]
    assert final_db.read_bytes() == decompressed_data
    assert meta["row_count"] == 2


def test_prepare_snapshot_failure_during_compression_preserves_existing(tmp_path):
    """If compression fails, existing outputs must remain unchanged."""
    out_dir = tmp_path / "output"
    _v1_meta = make_snapshot_bundle(out_dir, ["initial-pkg-v1"])
    v1_db_bytes = (out_dir / "pypi_data.sqlite").read_bytes()
    v1_meta_text = (out_dir / "pypi_data.sqlite.meta.json").read_text()
    v1_gz_bytes = (out_dir / "pypi_data.sqlite.gz").read_bytes()

    src_dir = tmp_path / "new_release"
    make_snapshot_bundle(src_dir, ["new-pkg-v2"], prefix="release")
    src_gz = src_dir / "release.sqlite.gz"
    src_meta_file = src_dir / "release.sqlite.meta.json"

    def mock_download_file(url: str, dest: Path) -> bool:
        if url.endswith(".sqlite.gz"):
            shutil.copyfile(src_gz, dest)
            return True
        elif url.endswith(".meta.json"):
            shutil.copyfile(src_meta_file, dest)
            return True
        return False

    def failing_compress(src: Path, dest: Path) -> None:
        raise RuntimeError("Simulated compression error: disk full")

    with (
        patch("prepare_snapshot.download_file", side_effect=mock_download_file),
        patch("prepare_snapshot.compress_gzip", side_effect=failing_compress),
    ):
        with pytest.raises(RuntimeError, match="Simulated compression error"):
            prepare_snapshot(refresh=False, output_dir=out_dir)

    # Assert existing outputs are completely untouched
    assert (out_dir / "pypi_data.sqlite").read_bytes() == v1_db_bytes
    assert (out_dir / "pypi_data.sqlite.meta.json").read_text() == v1_meta_text
    assert (out_dir / "pypi_data.sqlite.gz").read_bytes() == v1_gz_bytes


def test_prepare_snapshot_failure_during_validation_preserves_existing(tmp_path):
    """If staged validation fails, existing outputs must remain unchanged."""
    out_dir = tmp_path / "output"
    _v1_meta = make_snapshot_bundle(out_dir, ["initial-pkg-v1"])
    v1_db_bytes = (out_dir / "pypi_data.sqlite").read_bytes()
    v1_meta_text = (out_dir / "pypi_data.sqlite.meta.json").read_text()
    v1_gz_bytes = (out_dir / "pypi_data.sqlite.gz").read_bytes()

    src_dir = tmp_path / "new_release"
    make_snapshot_bundle(src_dir, ["new-pkg-v2"], prefix="release")
    src_gz = src_dir / "release.sqlite.gz"
    src_meta_file = src_dir / "release.sqlite.meta.json"

    def mock_download_file(url: str, dest: Path) -> bool:
        if url.endswith(".sqlite.gz"):
            shutil.copyfile(src_gz, dest)
            return True
        elif url.endswith(".meta.json"):
            shutil.copyfile(src_meta_file, dest)
            return True
        return False

    def failing_verify(staged_db: Path, staged_meta: Path, staged_gz: Path) -> dict:
        raise RuntimeError("Simulated validation failure: checksum mismatch")

    with (
        patch("prepare_snapshot.download_file", side_effect=mock_download_file),
        patch("prepare_snapshot.verify_staged_snapshot", side_effect=failing_verify),
    ):
        with pytest.raises(RuntimeError, match="Simulated validation failure"):
            prepare_snapshot(refresh=False, output_dir=out_dir)

    # Existing files untouched
    assert (out_dir / "pypi_data.sqlite").read_bytes() == v1_db_bytes
    assert (out_dir / "pypi_data.sqlite.meta.json").read_text() == v1_meta_text
    assert (out_dir / "pypi_data.sqlite.gz").read_bytes() == v1_gz_bytes


def test_prepare_snapshot_failure_during_replacement_rolls_back(tmp_path):
    """If replacement fails midway, rollback restores all pre-existing files."""
    out_dir = tmp_path / "output"
    make_snapshot_bundle(out_dir, ["initial-pkg-v1"])
    v1_db_bytes = (out_dir / "pypi_data.sqlite").read_bytes()
    v1_meta_text = (out_dir / "pypi_data.sqlite.meta.json").read_text()
    v1_gz_bytes = (out_dir / "pypi_data.sqlite.gz").read_bytes()

    src_dir = tmp_path / "new_release"
    make_snapshot_bundle(src_dir, ["new-pkg-v2"], prefix="release")
    src_gz = src_dir / "release.sqlite.gz"
    src_meta_file = src_dir / "release.sqlite.meta.json"

    def mock_download_file(url: str, dest: Path) -> bool:
        if url.endswith(".sqlite.gz"):
            shutil.copyfile(src_gz, dest)
            return True
        elif url.endswith(".meta.json"):
            shutil.copyfile(src_meta_file, dest)
            return True
        return False

    real_replace = os.replace
    replace_count = 0

    def fail_on_second_replace(src, dst):
        nonlocal replace_count
        replace_count += 1
        if replace_count == 2:
            raise OSError("Simulated filesystem I/O error on second replace")
        real_replace(src, dst)

    with (
        patch("prepare_snapshot.download_file", side_effect=mock_download_file),
        patch("os.replace", side_effect=fail_on_second_replace),
    ):
        with pytest.raises(OSError, match="Simulated filesystem I/O error"):
            prepare_snapshot(refresh=False, output_dir=out_dir)

    # Rollback must restore all three original files
    assert (out_dir / "pypi_data.sqlite").read_bytes() == v1_db_bytes
    assert (out_dir / "pypi_data.sqlite.meta.json").read_text() == v1_meta_text
    assert (out_dir / "pypi_data.sqlite.gz").read_bytes() == v1_gz_bytes


def test_prepare_snapshot_failure_during_indexing_preserves_existing(tmp_path):
    """If process_database fails, existing outputs must remain unchanged."""
    out_dir = tmp_path / "output"
    make_snapshot_bundle(out_dir, ["initial-pkg-v1"])
    v1_db_bytes = (out_dir / "pypi_data.sqlite").read_bytes()
    v1_meta_text = (out_dir / "pypi_data.sqlite.meta.json").read_text()
    v1_gz_bytes = (out_dir / "pypi_data.sqlite.gz").read_bytes()

    src_dir = tmp_path / "new_release"
    make_snapshot_bundle(src_dir, ["new-pkg-v2"], prefix="release")
    src_gz = src_dir / "release.sqlite.gz"
    src_meta_file = src_dir / "release.sqlite.meta.json"

    def mock_download_file(url: str, dest: Path) -> bool:
        if url.endswith(".sqlite.gz"):
            shutil.copyfile(src_gz, dest)
            return True
        elif url.endswith(".meta.json"):
            shutil.copyfile(src_meta_file, dest)
            return True
        return False

    def failing_process(src: Path, dest: Path, meta: Path) -> None:
        raise RuntimeError("Simulated failure during index creation")

    with (
        patch("prepare_snapshot.download_file", side_effect=mock_download_file),
        patch("prepare_snapshot.process_database", side_effect=failing_process),
    ):
        with pytest.raises(
            RuntimeError, match="Simulated failure during index creation"
        ):
            prepare_snapshot(refresh=False, output_dir=out_dir)

    # Existing files untouched
    assert (out_dir / "pypi_data.sqlite").read_bytes() == v1_db_bytes
    assert (out_dir / "pypi_data.sqlite.meta.json").read_text() == v1_meta_text
    assert (out_dir / "pypi_data.sqlite.gz").read_bytes() == v1_gz_bytes
