-- for most packages, first 20 chars is enough
CREATE INDEX idx_name_prefix ON projects (name (20));

-- full text indexes are way too slow on dolthub
-- ALTER TABLE projects ADD FULLTEXT INDEX idx_fulltext_name_summary (name);