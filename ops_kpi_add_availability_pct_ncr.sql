-- Add NCR-specific availability target (existing deployments).
INSERT INTO ops_kpi_targets (metric_key, value) VALUES
    ('availability_pct_ncr', 99.98)
ON CONFLICT (metric_key) DO NOTHING;
