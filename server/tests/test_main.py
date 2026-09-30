import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Add project root, server, and scripts directories to sys.path
repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "server"))
sys.path.insert(0, str(repo_root / "scripts"))
sys.path.insert(0, str(repo_root))
from build_search_index import build_indexes
from main import (
    app,
    escape_fts5_token,
    get_prefix_bounds,
)


def populate_test_db(db_path: Path):
    """Helper to create a fully indexed test database using production build_indexes."""
    conn = sqlite3.connect(db_path)
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

    packages = [
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
            "HTTP library with thread-safe connection pooling",
            "2024-02-16T00:00:00Z",
        ),
        ("fastapi", "0.110.0", "FastAPI framework", "2024-03-04T00:00:00Z"),
        (
            "uvicorn",
            "0.28.0",
            "The lightning-fast ASGI server.",
            "2024-03-09T00:00:00Z",
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
            "Core functionality for Pydantic validation",
            "2024-02-28T00:00:00Z",
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

    for p in packages:
        cursor.execute(
            """
            INSERT INTO projects (name, version, summary, upload_time, package_url, project_url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                p[0],
                p[1],
                p[2],
                p[3],
                f"https://pypi.org/project/{p[0]}/",
                f"https://pypi.org/project/{p[0]}/",
            ),
        )
    conn.commit()

    # Use production index building function directly
    build_indexes(conn)
    conn.close()


@pytest.fixture
def test_db_path(tmp_path):
    db_file = tmp_path / "test_pypi.sqlite"
    populate_test_db(db_file)
    return db_file


@pytest.fixture
def client(test_db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    with TestClient(app) as test_client:
        yield test_client


# --- Lifespan and Validation Tests ---


def test_lifespan_valid_db(test_db_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(test_db_path))
    with TestClient(app) as c:
        res = c.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"
        assert "sqlite_version" in res.json()


def test_lifespan_missing_db(tmp_path, monkeypatch):
    nonexistent = tmp_path / "does_not_exist.sqlite"
    monkeypatch.setenv("DB_PATH", str(nonexistent))
    with pytest.raises(RuntimeError, match="does not exist"), TestClient(app):
        pass


def test_lifespan_empty_projects(tmp_path, monkeypatch):
    empty_db = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(empty_db)
    conn.execute(
        "CREATE TABLE projects (name TEXT, version TEXT, summary TEXT, upload_time TEXT)"
    )
    conn.execute(
        "CREATE TABLE projects_search (project_rowid INTEGER, normalized_name TEXT)"
    )
    conn.execute("CREATE VIRTUAL TABLE projects_fts USING fts5(x)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(empty_db))
    with pytest.raises(RuntimeError, match="Projects table is empty"):
        with TestClient(app):
            pass


def test_lifespan_missing_columns(tmp_path, monkeypatch):
    bad_db = tmp_path / "bad_cols.sqlite"
    conn = sqlite3.connect(bad_db)
    conn.execute("CREATE TABLE projects (name TEXT)")
    conn.execute("INSERT INTO projects VALUES ('test')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(bad_db))
    with pytest.raises(RuntimeError, match="missing required columns"):
        with TestClient(app):
            pass


def test_lifespan_missing_search_table(tmp_path, monkeypatch):
    bad_db = tmp_path / "no_search.sqlite"
    conn = sqlite3.connect(bad_db)
    conn.execute(
        "CREATE TABLE projects (name TEXT, version TEXT, summary TEXT, upload_time TEXT)"
    )
    conn.execute("INSERT INTO projects VALUES ('test', '1.0', 'sum', '2024-01-01')")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(bad_db))
    with pytest.raises(RuntimeError, match="projects_search"), TestClient(app):
        pass


# --- Health Route Tests ---


def test_health_success(client):
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "sqlite_version" in data
    assert "arch" in data
    assert len(data["arch"]) > 0


def test_health_failure_when_db_removed(client, test_db_path):
    test_db_path.unlink()
    response = client.get("/health")
    assert response.status_code == 503


# --- Search Tests ---


def test_search_exact_match(client):
    response = client.get("/search?q=requests")
    assert response.status_code == 200
    results = response.json()
    assert len(results) >= 1
    assert results[0]["name"] == "requests"


def test_search_case_and_separator_normalization(client):
    response = client.get("/search?q=Requests_Mock")
    assert response.status_code == 200
    results = response.json()
    assert len(results) >= 1
    assert results[0]["name"] == "requests-mock"

    response2 = client.get("/search?q=foo-bar-baz")
    assert response2.status_code == 200
    results2 = response2.json()
    assert len(results2) >= 1
    assert results2[0]["name"] == "Foo.Bar_Baz"


def test_search_prefix_single_character(client):
    response = client.get("/search?q=a")
    assert response.status_code == 200
    names = [r["name"] for r in response.json()]
    assert "a" in names
    assert names[0] == "a"  # Exact match first


def test_search_prefix_two_characters(client):
    response = client.get("/search?q=ab")
    assert response.status_code == 200
    names = [r["name"] for r in response.json()]
    assert names[0] == "ab"  # Exact match first
    assert "abc-test" in names  # Prefix match


def test_search_prefix_multi_character(client):
    response = client.get("/search?q=req")
    assert response.status_code == 200
    names = [r["name"] for r in response.json()]
    assert names == ["requests", "requests-mock"]


def test_search_substring_trigram(client):
    # 'dantic' is not a prefix of pydantic, but trigram FTS matches it
    response = client.get("/search?q=dantic")
    assert response.status_code == 200
    names = [r["name"] for r in response.json()]
    assert "pydantic" in names
    assert "pydantic-core" in names


def test_search_internal_substring(client):
    # 'test' should match pytest, pytest-mock, abc-test
    response = client.get("/search?q=test")
    assert response.status_code == 200
    names = [r["name"] for r in response.json()]
    assert "abc-test" in names
    assert "pytest" in names
    assert "pytest-mock" in names


def test_search_deduplication_and_ranking(client):
    # 'pytest' is an exact match for pytest and prefix for pytest-mock
    response = client.get("/search?q=pytest")
    assert response.status_code == 200
    results = response.json()
    names = [r["name"] for r in results]
    assert names[0] == "pytest"  # Exact match ranked #1
    assert names[1] == "pytest-mock"  # Prefix ranked #2
    assert len(names) == len(set(names))  # Deduplicated


def test_search_blank_input(client):
    assert client.get("/search?q=").json() == []
    assert client.get("/search?q=   ").json() == []
    assert client.get("/search?q=---").json() == []


def test_search_special_chars_literal(client):
    # FTS characters, SQL wildcards, quotes should not cause 500 errors
    for special in ["***", "%", "_", "OR NOT", '"quotes"', "'single'"]:
        response = client.get(f"/search?q={special}")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


def test_get_prefix_bounds_unicode_scalars():
    # Ordinary ascii
    assert get_prefix_bounds("abc") == ("abc", "abd")

    # Boundary before surrogate range U+D7FF -> must jump to U+E000
    assert get_prefix_bounds("\ud7ff") == ("\ud7ff", "\ue000")
    assert get_prefix_bounds("pkg\ud7ff") == ("pkg\ud7ff", "pkg\ue000")

    # Start of BMP private use area U+E000 -> U+E001
    assert get_prefix_bounds("\ue000") == ("\ue000", "\ue001")
    assert get_prefix_bounds("pkg\ue000") == ("pkg\ue000", "pkg\ue001")

    # Maximum Unicode code point U+10FFFF
    assert get_prefix_bounds("\U0010ffff") == ("\U0010ffff", None)
    assert get_prefix_bounds("abc\U0010ffff") == ("abc\U0010ffff", "abd")
    assert get_prefix_bounds("abc\ud7ff\U0010ffff") == (
        "abc\ud7ff\U0010ffff",
        "abc\ue000",
    )
    assert get_prefix_bounds("\U0010ffff\U0010ffff") == ("\U0010ffff\U0010ffff", None)

    # Empty string
    assert get_prefix_bounds("") == ("", "")

    # Every calculated bound must be valid UTF-8
    for prefix in [
        "\ud7ff",
        "a\ud7ff",
        "\ue000",
        "z\ue000",
        "\U0010ffff",
        "a\U0010ffff",
    ]:
        low, up = get_prefix_bounds(prefix)
        low.encode("utf-8")
        if up is not None:
            up.encode("utf-8")


def test_search_unicode_max_codepoints(client):
    # Valid Unicode code points (surrogate boundaries, private use, max codepoint)
    for unicode_query in [
        "\ud7ff",
        "req\ud7ff",
        "\ud7ff\ud7ff",
        "\ue000",
        "pkg\ue000",
        "\U0010ffff",
        "req\U0010ffff",
        "abc\ud7ff\U0010ffff",
        "\U0010ffff\U0010ffff",
    ]:
        response = client.get(f"/search?q={unicode_query}")
        assert response.status_code == 200
        assert isinstance(response.json(), list)


def test_search_digit_heavy_names(client):
    response = client.get("/search?q=123")
    assert response.status_code == 200
    names = [r["name"] for r in response.json()]
    assert "123-tool" in names
    assert "tool_123" in names


def test_search_limit_validation(client):
    # Valid limits
    r1 = client.get("/search?q=test&limit=1")
    assert r1.status_code == 200
    assert len(r1.json()) == 1

    # Invalid limits: 422 Unprocessable Entity
    assert client.get("/search?q=test&limit=0").status_code == 422
    assert client.get("/search?q=test&limit=-5").status_code == 422
    assert client.get("/search?q=test&limit=101").status_code == 422


def test_search_query_length_validation(client):
    long_q = "a" * 101
    assert client.get(f"/search?q={long_q}").status_code == 422


# --- Package Details Route Tests ---


def test_get_package_exact(client):
    response = client.get("/package/requests")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "requests"
    assert data["version"] == "2.31.0"
    assert data["summary"] == "Python HTTP for Humans."
    assert data["package_url"] == "https://pypi.org/project/requests/"


def test_get_package_normalized_fallback(client):
    # Exact name in DB is 'Foo.Bar_Baz'
    response = client.get("/package/foo-bar-baz")
    assert response.status_code == 200
    assert response.json()["name"] == "Foo.Bar_Baz"

    response2 = client.get("/package/FOO_BAR_BAZ")
    assert response2.status_code == 200
    assert response2.json()["name"] == "Foo.Bar_Baz"


def test_get_package_not_found(client):
    response = client.get("/package/nonexistent-package-xyz")
    assert response.status_code == 404
    assert response.json()["detail"] == "Package not found"


# --- Query Plan and Index Tests ---


def test_query_plan_uses_btree_for_prefix(test_db_path):
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    lower, upper = get_prefix_bounds("req")
    cursor.execute(
        "EXPLAIN QUERY PLAN SELECT project_rowid FROM projects_search WHERE normalized_name >= ? AND normalized_name < ?",
        (lower, upper),
    )
    plan = " ".join(str(row) for row in cursor.fetchall())
    assert "INDEX" in plan or "COVERING INDEX" in plan
    conn.close()


def test_query_plan_uses_fts5_for_substring(test_db_path):
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    token = escape_fts5_token("dantic")
    cursor.execute(
        "EXPLAIN QUERY PLAN SELECT rowid FROM projects_fts WHERE projects_fts MATCH ?",
        (token,),
    )
    plan = " ".join(str(row) for row in cursor.fetchall())
    assert "projects_fts" in plan
    conn.close()


def test_ordinary_projects_query_without_extensions(test_db_path):
    """Verify external consumers can query projects table with standard SQLite without extensions."""
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name, version, summary FROM projects WHERE name = 'requests'"
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "requests"
    conn.close()
