BEGIN;

-- 1) Add the site-level territory column without touching existing rows.
ALTER TABLE site
    ADD COLUMN IF NOT EXISTS teritory TEXT NOT NULL DEFAULT '';

-- 2) Build a single deterministic source value per site_id.
--    Only site_ids with exactly one distinct non-blank territory are eligible.
WITH source_unique AS (
    SELECT
        site_id,
        MIN(BTRIM(territory)) AS territory_value
    FROM ops_kpi_availability
    WHERE NULLIF(BTRIM(territory), '') IS NOT NULL
    GROUP BY site_id
    HAVING COUNT(DISTINCT BTRIM(territory)) = 1
)
UPDATE site s
SET teritory = su.territory_value
FROM source_unique su
WHERE s.site_id = su.site_id
  AND NULLIF(BTRIM(s.teritory), '') IS NULL;

COMMIT;

-- Verification queries (run after commit).

-- A) Site row count baseline check (should remain unchanged before/after migration).
SELECT COUNT(*) AS site_row_count
FROM site;

-- B) Population status after backfill.
SELECT
    COUNT(*) FILTER (WHERE NULLIF(BTRIM(teritory), '') IS NULL) AS blank_teritory_rows,
    COUNT(*) FILTER (WHERE NULLIF(BTRIM(teritory), '') IS NOT NULL) AS populated_teritory_rows
FROM site;

-- C) Ambiguous source mappings intentionally skipped by the update.
SELECT
    site_id,
    COUNT(DISTINCT BTRIM(territory)) AS distinct_territory_count
FROM ops_kpi_availability
WHERE NULLIF(BTRIM(territory), '') IS NOT NULL
GROUP BY site_id
HAVING COUNT(DISTINCT BTRIM(territory)) > 1
ORDER BY distinct_territory_count DESC, site_id;
