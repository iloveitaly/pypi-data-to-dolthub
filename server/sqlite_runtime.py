"""SQLite runtime configuration and verification."""

import sqlite3

PINNED_SQLITE_VERSION = "3.53.1"
SEARCH_SCHEMA_VERSION = "1.0.0"


def parse_version(v: str) -> tuple[int, ...]:
    """Parse a semantic or dotted version string into a tuple of integers."""
    parts = []
    for piece in v.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if digits:
            parts.append(int(digits))
        else:
            break
    return tuple(parts[:3])


def verify_sqlite_runtime(conn: sqlite3.Connection | None = None) -> str:
    """
    Assert that the Python SQLite binding meets the pinned SQLite version
    and verify that the FTS5 trigram tokenizer is functional.
    """
    ver = sqlite3.sqlite_version
    current = parse_version(ver)
    required = parse_version(PINNED_SQLITE_VERSION)
    if current < required:
        raise RuntimeError(
            f"SQLite runtime version {ver} is older than required minimum {PINNED_SQLITE_VERSION}."
        )

    # Verify FTS5 trigram tokenizer capability
    test_conn = conn if conn is not None else sqlite3.connect(":memory:")
    try:
        cur = test_conn.cursor()
        cur.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _test_fts_trigram USING fts5(term, tokenize='trigram')"
        )
        cur.execute("INSERT INTO _test_fts_trigram(term) VALUES('runtime_probe_term')")
        cur.execute(
            "SELECT 1 FROM _test_fts_trigram WHERE _test_fts_trigram MATCH '\"probe\"'"
        )
        if not cur.fetchone():
            raise RuntimeError(
                "SQLite FTS5 trigram MATCH probe failed: no results returned."
            )
        cur.execute("DROP TABLE _test_fts_trigram")
    finally:
        if conn is None:
            test_conn.close()

    return ver
