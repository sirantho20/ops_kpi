-- One-off migration: site-first SIC/CM no longer require a matching ops_kpi_availability row.
-- Drops every foreign key that references ops_kpi_availability (safe to run multiple times).

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
