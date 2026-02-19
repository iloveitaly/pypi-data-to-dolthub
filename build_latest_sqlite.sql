-- Use disk-based spilling for larger-than-memory datasets
SET temp_directory = './duckdb_temp';
SET max_memory = '12GB';
SET enable_progress_bar = true;
SET preserve_insertion_order = false;

-- Create projects table by extracting the latest version per package based on upload time
CREATE TABLE projects AS
WITH releases AS (
    SELECT
        release->>'$.info.name' as name,
        version,
        release,
        -- Extract the latest upload time from the urls array using working list comprehension
        COALESCE(
            list_max([u.upload_time_iso_8601 FOR u IN from_json(release->'$.urls', '[{"upload_time_iso_8601": "VARCHAR"}]')]),
            '1970-01-01T00:00:00Z'
        ) as latest_upload_time
    FROM (
        SELECT
            unnest(map_keys(json)) as version,
            unnest(map_values(json)) as release
        FROM read_json('pypi_json_data/release_data/*/*/*.json',
            columns = {json: 'MAP(VARCHAR, JSON)'},
            maximum_object_size = 100000000
        )
    )
)
SELECT
    arg_max(name, latest_upload_time) as name,
    arg_max(version, latest_upload_time) as version,
    arg_max(release->>'$.info.author', latest_upload_time) as author,
    arg_max(release->>'$.info.author_email', latest_upload_time) as author_email,
    arg_max(release->>'$.info.home_page', latest_upload_time) as home_page,
    arg_max(release->>'$.info.license', latest_upload_time) as license,
    arg_max(release->>'$.info.maintainer', latest_upload_time) as maintainer,
    arg_max(release->>'$.info.maintainer_email', latest_upload_time) as maintainer_email,
    arg_max(release->>'$.info.package_url', latest_upload_time) as package_url,
    arg_max(release->>'$.info.platform', latest_upload_time) as platform,
    arg_max(release->>'$.info.project_url', latest_upload_time) as project_url,
    arg_max(release->>'$.info.requires_python', latest_upload_time) as requires_python,
    arg_max(release->>'$.info.summary', latest_upload_time) as summary,
    (arg_max(release->>'$.info.yanked', latest_upload_time))::BOOLEAN as yanked,
    arg_max(release->>'$.info.yanked_reason', latest_upload_time) as yanked_reason,
    from_json(arg_max(release->'$.info.classifiers', latest_upload_time), '["VARCHAR"]') as classifiers,
    from_json(arg_max(release->'$.info.requires_dist', latest_upload_time), '["VARCHAR"]') as requires_dist
FROM releases
GROUP BY name;

INSTALL sqlite;
LOAD sqlite;
ATTACH 'pypi_data.sqlite' AS sqlite_db (TYPE sqlite);
CREATE TABLE sqlite_db.projects AS SELECT * FROM projects;
