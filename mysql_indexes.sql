-- for most packages, first 20 chars is enough
CREATE INDEX idx_name_prefix ON projects (name (20));

-- for the big guns, let's use full text
ALTER TABLE projects ADD FULLTEXT INDEX idx_fulltext_name_summary (name, summary);