import os
import platform
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import structlog_config
import structlog_config.fastapi_access_logger
from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

try:
    from sqlite_runtime import verify_sqlite_runtime
except ImportError:
    from server.sqlite_runtime import verify_sqlite_runtime

# --- Logging ---
structlog_config.configure_logger()
logger = structlog.get_logger()

# --- Configuration ---
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "pypi_data.sqlite"
REQUIRED_COLUMNS = {"name", "version", "summary", "upload_time"}


def get_db_path() -> Path:
    override = os.getenv("DB_PATH")
    if override:
        return Path(override).resolve()
    return DEFAULT_DB_PATH


# --- Models ---
class SearchResult(BaseModel):
    name: str = Field(..., description="Package name as registered on PyPI")
    summary: str | None = Field(default=None, description="Package summary description")
    version: str | None = Field(
        default=None,
        description="Snapshot version based on latest upload time, not necessarily latest stable release",
    )
    upload_time: str | None = Field(
        default=None, description="Upload timestamp for this version"
    )


class PackageDetail(BaseModel):
    id: int | None = None
    name: str = Field(..., description="Package name as registered on PyPI")
    version: str | None = Field(
        default=None,
        description="Snapshot version based on latest upload time, not necessarily latest stable release",
    )
    author: str | None = None
    author_email: str | None = None
    home_page: str | None = None
    license: str | None = None
    maintainer: str | None = None
    maintainer_email: str | None = None
    package_url: str | None = None
    platform: str | None = None
    project_url: str | None = None
    requires_python: str | None = None
    summary: str | None = None
    upload_time: str | None = None
    yanked: int | None = 0
    yanked_reason: str | None = None
    classifiers: str | None = None
    requires_dist: str | None = None


# --- Helpers ---
def normalize_name(name: str) -> str:
    """PEP 503 normalization: lowercase, collapse runs of '-', '_', '.' to '-'."""
    if not name:
        return ""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def get_prefix_bounds(prefix: str) -> tuple[str, str | None]:
    """
    Calculate upper bound for range-based prefix query on B-tree index.
    Returns (lower_bound, upper_bound). If prefix consists entirely of maximum
    Unicode code points (0x10FFFF), upper_bound is None.
    Skips Unicode surrogate code points (0xD800-0xDFFF) to ensure upper_bound
    is always a valid UTF-8 encodable Unicode scalar.
    """
    if not prefix:
        return "", ""
    for i in range(len(prefix) - 1, -1, -1):
        code = ord(prefix[i])
        if code < 0xD7FF:
            upper = prefix[:i] + chr(code + 1)
            return prefix, upper
        elif code < 0xE000:
            upper = prefix[:i] + chr(0xE000)
            return prefix, upper
        elif code < 0x10FFFF:
            upper = prefix[:i] + chr(code + 1)
            return prefix, upper
    return prefix, None


def escape_fts5_token(token: str) -> str:
    """Escape query token for literal matching in FTS5 trigram index."""
    return '"' + token.replace('"', '""') + '"'


def validate_db(path: Path):
    """Validate SQLite snapshot integrity, schema, and search indexes during startup."""
    if not path.is_file():
        raise RuntimeError(f"Database file does not exist at {path}")

    # Verify SQLite runtime capability
    verify_sqlite_runtime()

    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as e:
        raise RuntimeError(f"Cannot open database file at {path}: {e}") from e

    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check(1)")
        res = cursor.fetchone()
        if not res or res[0] != "ok":
            raise RuntimeError(f"Database integrity check failed: {res}")

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

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects_search'"
        )
        if not cursor.fetchone():
            raise RuntimeError("Database missing required 'projects_search' table")

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='projects_fts'"
        )
        if not cursor.fetchone():
            raise RuntimeError("Database missing required 'projects_fts' virtual table")

        # Probe FTS5 trigram index with an indexed token
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
    finally:
        conn.close()


# --- Database Dependency ---
def get_db():
    path = get_db_path()
    if not path.is_file():
        raise HTTPException(status_code=503, detail=f"Database file not found: {path}")

    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        logger.error(f"Failed to open database: {e}")
        raise HTTPException(status_code=503, detail="Database connection failed") from e

    try:
        yield conn
    finally:
        conn.close()


# --- Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = get_db_path()
    logger.info(f"Validating database at {db_path}")
    validate_db(db_path)
    logger.info("Database validation passed successfully")
    yield


app = FastAPI(
    title="PyPI SQLite Search API",
    description="Fast, read-only PyPI package search and metadata service backed by SQLite.",
    version="0.1.0",
    lifespan=lifespan,
)
structlog_config.fastapi_access_logger.add_middleware(app)


# --- Routes ---
@app.get("/health", summary="Health check")
def health(db: sqlite3.Connection = Depends(get_db)):
    """Verifies database accessibility and search index readiness."""
    try:
        cursor = db.cursor()
        cursor.execute("SELECT 1 FROM projects LIMIT 1")
        if not cursor.fetchone():
            raise HTTPException(status_code=503, detail="Projects table empty")
        cursor.execute("SELECT 1 FROM projects_search LIMIT 1")
        if not cursor.fetchone():
            raise HTTPException(status_code=503, detail="Search index empty")

        # Verify FTS5 trigram index via read MATCH probe
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
                raise HTTPException(
                    status_code=503, detail="FTS5 search index desynchronized or empty"
                )

        return {
            "status": "ok",
            "sqlite_version": sqlite3.sqlite_version,
            "arch": platform.machine(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(
            status_code=503, detail="Database or search index unavailable"
        ) from e


@app.get(
    "/search",
    response_model=list[SearchResult],
    summary="Search PyPI packages",
    description="Ranked package search returning exact matches, prefixes, and substrings. Snapshot versions follow upload time.",
)
def search_packages(
    q: str = Query(
        ..., max_length=100, description="Package search query (max 100 chars)"
    ),
    limit: int = Query(
        50, ge=1, le=100, description="Maximum number of results to return (1-100)"
    ),
    db: sqlite3.Connection = Depends(get_db),
) -> list[SearchResult]:
    if not q.strip():
        return []

    norm_q = normalize_name(q)
    if not norm_q:
        return []

    cursor = db.cursor()
    matched_rowids: list[int] = []
    seen_ids: set[int] = set()

    # 1. Exact match on normalized_name
    cursor.execute(
        "SELECT project_rowid FROM projects_search WHERE normalized_name = ?",
        (norm_q,),
    )
    exact_row = cursor.fetchone()
    if exact_row:
        rid = exact_row["project_rowid"]
        matched_rowids.append(rid)
        seen_ids.add(rid)

    # 2. Prefix match using indexed B-tree range (if limit slots remain)
    if len(matched_rowids) < limit:
        remaining = limit - len(matched_rowids)
        lower_bound, upper_bound = get_prefix_bounds(norm_q)
        if upper_bound is not None:
            cursor.execute(
                """
                SELECT project_rowid FROM projects_search
                WHERE normalized_name >= ? AND normalized_name < ? AND normalized_name != ?
                ORDER BY normalized_name ASC
                LIMIT ?
                """,
                (lower_bound, upper_bound, norm_q, remaining),
            )
        else:
            cursor.execute(
                """
                SELECT project_rowid FROM projects_search
                WHERE normalized_name >= ? AND normalized_name != ?
                ORDER BY normalized_name ASC
                LIMIT ?
                """,
                (lower_bound, norm_q, remaining),
            )
        for row in cursor.fetchall():
            rid = row["project_rowid"]
            if rid not in seen_ids:
                matched_rowids.append(rid)
                seen_ids.add(rid)

    # 3. Substring match via FTS5 trigram (if limit slots remain and length >= 3)
    if len(matched_rowids) < limit and len(norm_q) >= 3:
        remaining = limit - len(matched_rowids)
        fts_token = escape_fts5_token(norm_q)
        cursor.execute(
            """
            SELECT s.project_rowid
            FROM projects_fts f
            JOIN projects_search s ON s.project_rowid = f.rowid
            WHERE f.projects_fts MATCH ?
            ORDER BY s.normalized_name ASC
            LIMIT ?
            """,
            (fts_token, remaining + len(seen_ids)),
        )
        for row in cursor.fetchall():
            rid = row["project_rowid"]
            if rid not in seen_ids:
                matched_rowids.append(rid)
                seen_ids.add(rid)
                if len(matched_rowids) >= limit:
                    break

    if not matched_rowids:
        return []

    # Fetch project details in bulk, maintaining matched_rowids order
    placeholders = ",".join("?" for _ in matched_rowids)
    cursor.execute(
        f"SELECT rowid, name, summary, version, upload_time FROM projects WHERE rowid IN ({placeholders})",
        matched_rowids,
    )
    rows_by_id = {row["rowid"]: row for row in cursor.fetchall()}

    results = []
    for rid in matched_rowids:
        if rid in rows_by_id:
            r = rows_by_id[rid]
            results.append(
                SearchResult(
                    name=r["name"],
                    summary=r["summary"],
                    version=r["version"],
                    upload_time=r["upload_time"],
                )
            )

    return results


@app.get(
    "/package/{name}",
    response_model=PackageDetail,
    summary="Get package details",
    description="Lookup package details by exact name or normalized name.",
)
def get_package(name: str, db: sqlite3.Connection = Depends(get_db)) -> PackageDetail:
    cursor = db.cursor()
    # 1. Exact name lookup
    cursor.execute("SELECT * FROM projects WHERE name = ?", (name,))
    row = cursor.fetchone()

    # 2. Normalized name lookup fallback
    if not row:
        norm_name = normalize_name(name)
        cursor.execute(
            """
            SELECT p.* FROM projects_search s
            JOIN projects p ON p.rowid = s.project_rowid
            WHERE s.normalized_name = ?
            """,
            (norm_name,),
        )
        row = cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Package not found")

    return PackageDetail(**dict(row))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
