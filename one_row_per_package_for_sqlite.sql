-- Create a new table with the query results, including row_num temporarily
CREATE TABLE projects_new AS
WITH ranked_projects AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY name
               ORDER BY
                   CAST(SUBSTR(version, 1, INSTR(version || '.', '.') - 1) AS INTEGER) DESC,
                   CAST(
                       CASE
                           WHEN INSTR(version, '.') > 0 THEN
                               SUBSTR(version, INSTR(version, '.') + 1, 
                                      INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') - 1)
                           ELSE '0'
                       END AS INTEGER
                   ) DESC,
                   CAST(
                       CASE
                           WHEN INSTR(version, '.') > 0 
                            AND INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') > 0 THEN
                               SUBSTR(
                                   SUBSTR(version || '..', INSTR(version, '.') + 1),
                                   INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') + 1,
                                   INSTR(SUBSTR(SUBSTR(version || '..', INSTR(version, '.') + 1) || '.', 
                                         INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') + 1), '.') - 1
                               )
                           ELSE '0'
                       END AS INTEGER
                   ) DESC,
                   CAST(
                       CASE
                           WHEN INSTR(version, '.') > 0 
                            AND INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') > 0 
                            AND INSTR(SUBSTR(SUBSTR(version || '..', INSTR(version, '.') + 1) || '..', 
                                              INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') + 1), '.') > 0 THEN
                               SUBSTR(
                                   SUBSTR(SUBSTR(version || '..', INSTR(version, '.') + 1) || '..', 
                                          INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') + 1),
                                   INSTR(SUBSTR(SUBSTR(version || '..', INSTR(version, '.') + 1) || '..', 
                                                 INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') + 1), '.') + 1,
                                   INSTR(SUBSTR(SUBSTR(SUBSTR(version || '..', INSTR(version, '.') + 1) || '..', 
                                                       INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') + 1) || '.', 
                                         INSTR(SUBSTR(SUBSTR(version || '..', INSTR(version, '.') + 1) || '..', 
                                                       INSTR(SUBSTR(version || '..', INSTR(version, '.') + 1), '.') + 1), '.') + 1), '.') - 1
                               )
                           ELSE '0'
                       END AS INTEGER
                   ) DESC,
                   id DESC
           ) AS row_num
    FROM projects
)
SELECT * 
FROM ranked_projects 
WHERE row_num = 1;

-- Remove the row_num column from the new table
ALTER TABLE projects_new DROP COLUMN row_num;

-- Drop the original projects table
DROP TABLE projects;

-- Rename the new table to projects
ALTER TABLE projects_new RENAME TO projects;