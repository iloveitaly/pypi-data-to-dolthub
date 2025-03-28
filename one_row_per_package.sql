SELECT p1.*
FROM projects p1
WHERE p1.id = (
    SELECT p2.id
    FROM projects p2
    WHERE p2.name = p1.name
    ORDER BY
        CAST(SUBSTRING_INDEX(p2.version, '.', 1) AS UNSIGNED) DESC,
        CAST(IF(LOCATE('.', p2.version) > 0, SUBSTRING_INDEX(SUBSTRING_INDEX(p2.version, '.', 2), '.', -1), 0) AS UNSIGNED) DESC,
        CAST(IF(LOCATE('.', p2.version, LOCATE('.', p2.version) + 1) > 0, SUBSTRING_INDEX(SUBSTRING_INDEX(p2.version, '.', 3), '.', -1), 0) AS UNSIGNED) DESC,
        CAST(IF(LOCATE('.', p2.version, LOCATE('.', p2.version, LOCATE('.', p2.version) + 1) + 1) > 0, SUBSTRING_INDEX(SUBSTRING_INDEX(p2.version, '.', 4), '.', -1), 0) AS UNSIGNED) DESC,
        p2.id DESC
    LIMIT 1
);