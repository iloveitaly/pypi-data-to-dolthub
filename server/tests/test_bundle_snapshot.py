"""Tests for snapshot bundling and pre-build validation."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "scripts"))

from build_search_index import process_database
from bundle_snapshot import bundle_snapshot
from prepare_snapshot import compress_gzip


def create_indexed_snapshot(
    dest_dir: Path, name: str, packages: list[str]
) -> tuple[Path, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    raw_path = dest_dir / f"{name}.raw.sqlite"
    db_path = dest_dir / f"{name}.sqlite"
    meta_path = dest_dir / f"{name}.sqlite.meta.json"

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
    return db_path, meta_path


def test_bundle_snapshot_copies_root_to_server(tmp_path):
    root_dir = tmp_path / "root"
    server_dir = tmp_path / "server"

    root_db, root_meta = create_indexed_snapshot(
        root_dir, "pypi_data", ["root-pkg-1", "root-pkg-2"]
    )
    server_db, server_meta = create_indexed_snapshot(
        server_dir, "pypi_data", ["old-server-pkg"]
    )

    old_server_bytes = server_db.read_bytes()

    # Bundle root snapshot into server context
    result_meta = bundle_snapshot(source_db=root_db, dest_dir=server_dir)

    assert result_meta["row_count"] == 2
    assert server_db.read_bytes() == root_db.read_bytes()
    assert server_db.read_bytes() != old_server_bytes

    # Server metadata is updated to match root
    server_meta_data = json.loads(server_meta.read_text())
    assert server_meta_data["sha256"] == result_meta["sha256"]


def test_bundle_snapshot_missing_source_fails(tmp_path):
    server_dir = tmp_path / "server"
    server_db, server_meta = create_indexed_snapshot(
        server_dir, "pypi_data", ["server-pkg"]
    )
    server_bytes = server_db.read_bytes()

    missing_db = tmp_path / "nonexistent.sqlite"

    with pytest.raises(FileNotFoundError, match="Source snapshot database not found"):
        bundle_snapshot(source_db=missing_db, dest_dir=server_dir)

    # Server files are completely preserved
    assert server_db.read_bytes() == server_bytes


def test_bundle_snapshot_checksum_mismatch_fails(tmp_path):
    root_dir = tmp_path / "root"
    server_dir = tmp_path / "server"

    root_db, root_meta = create_indexed_snapshot(root_dir, "pypi_data", ["root-pkg"])
    server_db, server_meta = create_indexed_snapshot(
        server_dir, "pypi_data", ["server-pkg"]
    )
    server_bytes = server_db.read_bytes()

    # Corrupt root db by appending a byte
    with open(root_db, "ab") as f:
        f.write(b"\x00")

    with pytest.raises(ValueError, match="Snapshot checksum mismatch"):
        bundle_snapshot(source_db=root_db, dest_dir=server_dir)

    # Server snapshot unchanged
    assert server_db.read_bytes() == server_bytes


def test_bundle_snapshot_decompresses_gz_if_uncompressed_missing(tmp_path):
    root_dir = tmp_path / "root"
    server_dir = tmp_path / "server"

    root_db, root_meta = create_indexed_snapshot(
        root_dir, "pypi_data", ["gz-packaged-pkg"]
    )
    root_gz = root_dir / "pypi_data.sqlite.gz"
    compress_gzip(root_db, root_gz)

    # Delete uncompressed source db
    root_db.unlink()
    assert not root_db.exists()

    result_meta = bundle_snapshot(source_db=root_db, dest_dir=server_dir)
    assert result_meta["row_count"] == 1

    server_db = server_dir / "pypi_data.sqlite"
    assert server_db.is_file()
    assert (
        result_meta["sha256"]
        == json.loads((server_dir / "pypi_data.sqlite.meta.json").read_text())["sha256"]
    )
