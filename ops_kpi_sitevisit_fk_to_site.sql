-- =============================================================================
-- ops_kpi_sitevisit: drop FK to ops_kpi_availability, normalize site_id to
-- public.site.site_id, merge duplicate (site_id, date) with SUM(visit_count),
-- add FK to public.site (site_id).
--
-- Run AFTER public.site exists. Safe to re-run: drops/recreates named FK only.
--
-- -----------------------------------------------------------------------------
-- BACKUP / RESTORE (no data loss) — run before this script
-- -----------------------------------------------------------------------------
--
-- 1) Logical dump (restore with pg_restore or psql -f depending on format):
--    pg_dump "$DATABASE_URL" --no-owner --table=ops_kpi_sitevisit -Fc -f ops_kpi_sitevisit_pre_fk.dump
--    # or plain SQL:
--    pg_dump "$DATABASE_URL" --no-owner --table=ops_kpi_sitevisit -f ops_kpi_sitevisit_pre_fk.sql
--
-- 2) In-database copy (quick rollback):
--    DROP TABLE IF EXISTS ops_kpi_sitevisit_backup_pre_fk;
--    CREATE TABLE ops_kpi_sitevisit_backup_pre_fk AS TABLE ops_kpi_sitevisit WITH DATA;
--
--    Restore from copy if needed:
--    TRUNCATE ops_kpi_sitevisit;
--    INSERT INTO ops_kpi_sitevisit SELECT * FROM ops_kpi_sitevisit_backup_pre_fk;
--    -- re-run any constraints/indexes that were dropped
--
-- 3) Checksums — record before migration; after migration these should match unless
--    you intentionally merged duplicate keys (then totals must still match):
--    SELECT COUNT(*) AS n_rows, COALESCE(SUM(visit_count), 0)::bigint AS sum_visits
--    FROM ops_kpi_sitevisit;
--
-- 4) Dry-run in a transaction: wrap the BEGIN…COMMIT block below, use ROLLBACK
--    first to validate; then re-run with COMMIT.
-- =============================================================================

-- Pre-migration checksums (optional; run manually and save output)
-- SELECT COUNT(*) AS n_rows, COALESCE(SUM(visit_count), 0)::bigint AS sum_visits FROM ops_kpi_sitevisit;

BEGIN;

-- Drop every FK that references ops_kpi_availability (idempotent).
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.conname AS cname, c.conrelid::regclass AS tbl
        FROM pg_constraint c
        WHERE c.confrelid = 'ops_kpi_availability'::regclass
          AND c.contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT IF EXISTS %I', r.tbl, r.cname);
    END LOOP;
END $$;

-- Optional diagnostic: sitevisit rows whose key does not match any site.site_id and
-- do not match the KPI key expression (uncomment to run).
-- SELECT v.*
-- FROM ops_kpi_sitevisit v
-- WHERE NOT EXISTS (
--     SELECT 1 FROM site s
--     WHERE v.site_id::text = s.site_id::text
-- )
-- AND NOT EXISTS (
--     SELECT 1 FROM site s
--     WHERE v.site_id::text = COALESCE(NULLIF(BTRIM(s.pla_id::text), ''), s.site_id::text)
-- );

-- Map KPI-style keys (PLA when present, else site PK) to canonical site.site_id.
UPDATE ops_kpi_sitevisit v
SET site_id = s.site_id::text
FROM site s
WHERE v.site_id::text = COALESCE(NULLIF(BTRIM(s.pla_id::text), ''), s.site_id::text)
  AND v.site_id::text IS DISTINCT FROM s.site_id::text;

-- Remaining KPI keys that do not match site.pla_id/site_id (e.g. PLA typos in facts vs site)
-- still align with availability on (site_id, date); map via ptci_number -> site.site_id.
UPDATE ops_kpi_sitevisit v
SET site_id = s.site_id::text
FROM ops_kpi_availability a
INNER JOIN site s ON s.site_id = NULLIF(BTRIM(a.ptci_number::text), '')
WHERE v.site_id = a.site_id
  AND v.date = a.date
  AND v.site_id::text IS DISTINCT FROM s.site_id::text;

-- Collapse duplicate (site_id, date) preserving total visit_count (idempotent).
CREATE TEMP TABLE _ops_kpi_sitevisit_merged ON COMMIT DROP AS
SELECT site_id, date, SUM(visit_count)::integer AS visit_count
FROM ops_kpi_sitevisit
GROUP BY site_id, date;

TRUNCATE ops_kpi_sitevisit;
INSERT INTO ops_kpi_sitevisit (site_id, date, visit_count)
SELECT site_id, date, visit_count FROM _ops_kpi_sitevisit_merged;

-- Fail if any row cannot reference site.
DO $$
DECLARE
    n bigint;
BEGIN
    SELECT COUNT(*) INTO n
    FROM ops_kpi_sitevisit v
    WHERE NOT EXISTS (
        SELECT 1 FROM site s WHERE s.site_id::text = v.site_id::text
    );
    IF n > 0 THEN
        RAISE EXCEPTION 'ops_kpi_sitevisit has % rows with no matching site.site_id; fix data before FK', n;
    END IF;
END $$;

ALTER TABLE ops_kpi_sitevisit
    DROP CONSTRAINT IF EXISTS fk_ops_kpi_sitevisit_site;

ALTER TABLE ops_kpi_sitevisit
    ADD CONSTRAINT fk_ops_kpi_sitevisit_site
    FOREIGN KEY (site_id) REFERENCES public.site (site_id);

COMMIT;

-- Post-migration checksums (optional; must match pre-migration unless audited merge):
-- SELECT COUNT(*) AS n_rows, COALESCE(SUM(visit_count), 0)::bigint AS sum_visits FROM ops_kpi_sitevisit;
