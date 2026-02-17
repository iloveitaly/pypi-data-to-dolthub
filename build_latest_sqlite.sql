INSTALL httpfs;
LOAD httpfs;
SET enable_progress_bar = true;
SET preserve_insertion_order = false;

-- Increase memory limit if possible, standard runners have ~7GB
SET max_memory = '6GB';

-- Create projects table directly from remote JSON files
-- We use the GH raw URL pattern. Note that DuckDB supports globbing over HTTP.
-- However, globbing hundreds of thousands of files over HTTP might be slow.
-- We'll try to use the same logic but pointing to the raw content.
CREATE TABLE projects AS
WITH all_versions AS (
    SELECT
        release->>'$.info.name' as name,
        version,
        release->>'$.info.author' as author,
        release->>'$.info.author_email' as author_email,
        release->>'$.info.home_page' as home_page,
        release->>'$.info.license' as license,
        release->>'$.info.maintainer' as maintainer,
        release->>'$.info.maintainer_email' as maintainer_email,
        release->>'$.info.package_url' as package_url,
        release->>'$.info.platform' as platform,
        release->>'$.info.project_url' as project_url,
        release->>'$.info.requires_python' as requires_python,
        release->>'$.info.summary' as summary,
        (release->>'$.info.yanked')::BOOLEAN as yanked,
        release->>'$.info.yanked_reason' as yanked_reason,
        from_json(release->'$.info.classifiers', '["VARCHAR"]') as classifiers,
        from_json(release->'$.info.requires_dist', '["VARCHAR"]') as requires_dist
    FROM (
        SELECT
            unnest(map_keys(json)) as version,
            unnest(map_values(json)) as release
        FROM read_json('https://raw.githubusercontent.com/pypi-data/pypi-json-data/main/release_data/*/*/*.json',
            columns = {json: 'MAP(VARCHAR, JSON)'},
            maximum_object_size = 100000000
        )
    )
),
latest_versions AS (
    SELECT 
        *,
        ROW_NUMBER() OVER (PARTITION BY name ORDER BY version DESC) as rn
    FROM all_versions
)
SELECT * EXCLUDE (rn)
FROM latest_versions
WHERE rn = 1;

INSTALL sqlite;
LOAD sqlite;
ATTACH 'pypi_data.sqlite' AS sqlite_db (TYPE sqlite);
CREATE TABLE sqlite_db.projects AS SELECT * FROM projects;
