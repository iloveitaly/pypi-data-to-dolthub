-- DuckDB configuration for efficient processing
SET temp_directory = './duckdb_temp';
SET max_memory = '12GB';
SET enable_progress_bar = true;
SET preserve_insertion_order = false;

-- Create projects table from BigQuery export
CREATE TABLE projects AS
SELECT
    -- id column is expected by the previous schema (though it was nullable)
    NULL::INT as id,
    name,
    version,
    author,
    author_email,
    home_page,
    license,
    maintainer,
    maintainer_email,
    'https://pypi.org/project/' || name || '/' || version || '/' as package_url,
    -- BigQuery distribution_metadata has a platform field but it's often file-specific.
    -- The original schema included this, so we'll keep it as null or extract it if needed.
    NULL::VARCHAR as platform,
    'https://pypi.org/project/' || name || '/' as project_url,
    requires_python,
    summary,
    upload_time,
    -- BigQuery doesn't easily expose yanked status in the public distribution_metadata table
    0 as yanked,
    NULL::VARCHAR as yanked_reason,
    -- Convert LISTs from Parquet to JSON strings to match the Dolt 'text' schema
    to_json(classifiers)::VARCHAR as classifiers,
    to_json(requires_dist)::VARCHAR as requires_dist
FROM read_parquet('pypi_metadata.parquet');

-- Export to SQLite
INSTALL sqlite;
LOAD sqlite;
ATTACH 'pypi_data.sqlite' AS sqlite_db (TYPE sqlite);
CREATE TABLE sqlite_db.projects AS SELECT * FROM projects;
