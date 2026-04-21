-- Operations KPI: normalized daily metrics (site_id = normalized natural site id, same values as Python site_key).

CREATE TABLE IF NOT EXISTS ops_kpi_availability (
    site_id TEXT NOT NULL,
    date DATE NOT NULL,
    pla_id TEXT,
    ptci_number TEXT,
    region TEXT NOT NULL,
    zoo TEXT NOT NULL,
    territory TEXT NOT NULL DEFAULT '',
    incident_count DOUBLE PRECISION,
    outage_mins DOUBLE PRECISION,
    accepted_outage_minutes DOUBLE PRECISION,
    availability DOUBLE PRECISION,
    uptime_per_tenant DOUBLE PRECISION,
    total_available_minutes DOUBLE PRECISION,
    PRIMARY KEY (site_id, date)
);

-- site_id is canonical public.site.site_id (not PLA). FK is added in ops_kpi_sitevisit_fk_to_site.sql
-- after public.site exists; greenfield DBs should run that migration once site is loaded.
CREATE TABLE IF NOT EXISTS ops_kpi_sitevisit (
    site_id TEXT NOT NULL,
    date DATE NOT NULL,
    visit_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (site_id, date)
);

CREATE TABLE IF NOT EXISTS ops_kpi_cm (
    site_id TEXT NOT NULL,
    date DATE NOT NULL,
    cm_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (site_id, date)
);

CREATE INDEX IF NOT EXISTS idx_ops_kpi_availability_date ON ops_kpi_availability (date);
CREATE INDEX IF NOT EXISTS idx_ops_kpi_availability_region_date ON ops_kpi_availability (region, date);

-- Global KPI parameters: per-metric baseline factors (prev FY total × factor) + fixed MTTR/availability.
CREATE TABLE IF NOT EXISTS ops_kpi_targets (
    metric_key TEXT PRIMARY KEY,
    value DOUBLE PRECISION NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO ops_kpi_targets (metric_key, value) VALUES
    ('events_baseline_factor', 0.85),
    ('cm_baseline_factor', 0.85),
    ('visit_baseline_factor', 0.85),
    ('mttr_minutes', 200),
    ('availability_pct', 99.96),
    ('availability_pct_ncr', 99.98)
ON CONFLICT (metric_key) DO NOTHING;
