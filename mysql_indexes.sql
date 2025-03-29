-- for most packages, first 20 chars is enough
CREATE INDEX idx_name_prefix ON projects (name (20));

-- https://discord.com/channels/746150696465727668/746152112169287760/1355536969475555549
-- full text indexes are way too slow on dolthub
-- ALTER TABLE projects ADD FULLTEXT INDEX idx_fulltext_name_summary (name);