-- Use disk-based spilling for larger-than-memory datasets
SET temp_directory = './duckdb_temp';
SET max_memory = '12GB'; -- Standard runners have 16GB, let's use most of it since we cleared disk swap
SET enable_progress_bar = true;
SET preserve_insertion_order = false;

-- Create projects table by extracting the latest version per file
CREATE TABLE projects AS
SELECT
    arg_max(release->>'$.info.name', version) as name,
    arg_max(version, version) as version,
    arg_max(release->>'$.info.author', version) as author,
    arg_max(release->>'$.info.author_email', version) as author_email,
    arg_max(release->>'$.info.home_page', version) as home_page,
    arg_max(release->>'$.info.license', version) as license,
    arg_max(release->>'$.info.maintainer', version) as maintainer,
    arg_max(release->>'$.info.maintainer_email', version) as maintainer_email,
    arg_max(release->>'$.info.package_url', version) as package_url,
    arg_max(release->>'$.info.platform', version) as platform,
    arg_max(release->>'$.info.project_url', version) as project_url,
    arg_max(release->>'$.info.requires_python', version) as requires_python,
    arg_max(release->>'$.info.summary', version) as summary,
    (arg_max(release->>'$.info.yanked', version))::BOOLEAN as yanked,
    arg_max(release->>'$.info.yanked_reason', version) as yanked_reason,
    from_json(arg_max(release->'$.info.classifiers', version), '["VARCHAR"]') as classifiers,
    from_json(arg_max(release->'$.info.requires_dist', version), '["VARCHAR"]') as requires_dist
FROM (
    SELECT
        unnest(map_keys(json)) as version,
        unnest(map_values(json)) as release
    FROM read_json('pypi_json_data/release_data/*/*/*.json',
        columns = {json: 'MAP(VARCHAR, JSON)'},
        maximum_object_size = 100000000
    )
)
-- Each file contains data for one package, but the glob covers all files.
-- We group by name to collapse multiple versions into the latest one.
GROUP BY release->>'$.info.name';

INSTALL sqlite;
LOAD sqlite;
ATTACH 'pypi_data.sqlite' AS sqlite_db (TYPE sqlite);
CREATE TABLE sqlite_db.projects AS SELECT * FROM projects;
