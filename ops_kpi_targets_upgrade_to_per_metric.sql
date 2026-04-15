-- Run once on databases that still have legacy `baseline_factor` row.
-- Upserts per-metric targets and removes the old single baseline_factor key.

INSERT INTO ops_kpi_targets (metric_key, value) VALUES
    ('events_baseline_factor', 0.85),
    ('cm_baseline_factor', 0.85),
    ('visit_baseline_factor', 0.85),
    ('mttr_minutes', 200),
    ('availability_pct', 99.96)
ON CONFLICT (metric_key) DO UPDATE SET
    value = EXCLUDED.value,
    updated_at = now();

DELETE FROM ops_kpi_targets WHERE metric_key = 'baseline_factor';
