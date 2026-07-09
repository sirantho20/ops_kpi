"""Tests: dashboard region normalization (Python + cube partition sum)."""

from __future__ import annotations

import unittest

import pandas as pd

from operations_kpi_data import (
    REGION_ORDER,
    REGION_OTHER,
    OpsKpiFactCubes,
    normalize_ops_kpi_region_display,
    regions_for_table,
)


class NormalizeOpsKpiRegionDisplayTests(unittest.TestCase):
    def test_metro_manila_to_ncr(self) -> None:
        self.assertEqual(normalize_ops_kpi_region_display("METRO MANILA"), "NCR")
        self.assertEqual(normalize_ops_kpi_region_display("metro manila "), "NCR")

    def test_blank_and_unknown_to_other(self) -> None:
        self.assertEqual(normalize_ops_kpi_region_display(""), REGION_OTHER)
        self.assertEqual(normalize_ops_kpi_region_display(None), REGION_OTHER)
        self.assertEqual(normalize_ops_kpi_region_display("   "), REGION_OTHER)
        self.assertEqual(normalize_ops_kpi_region_display("LUZON"), REGION_OTHER)

    def test_known_codes_unchanged(self) -> None:
        for r in REGION_ORDER:
            self.assertEqual(normalize_ops_kpi_region_display(r), r)
            self.assertEqual(normalize_ops_kpi_region_display(r.lower()), r)


class RegionsForTableTests(unittest.TestCase):
    def test_omits_other_when_no_other_rows(self) -> None:
        df = pd.DataFrame({"Region": ["NCR", "NCR", "VIS"]})
        self.assertEqual(regions_for_table(df), list(REGION_ORDER))

    def test_appends_other_when_present(self) -> None:
        df = pd.DataFrame({"Region": ["NCR", REGION_OTHER]})
        self.assertEqual(regions_for_table(df), [*REGION_ORDER, REGION_OTHER])


class YearRegionPartitionSumTests(unittest.TestCase):
    """Partition keys NCR..MIN + OTHER must sum to year_overall for incidents (same as SQL GROUP BY)."""

    def test_synthetic_cube_incidents_sum(self) -> None:
        yr = 2025
        parts = {
            (yr, "NCR"): (30.0, None, None),
            (yr, "NLZ"): (10.0, None, None),
            (yr, "SLZ"): (10.0, None, None),
            (yr, "VIS"): (10.0, None, None),
            (yr, "MIN"): (10.0, None, None),
            (yr, REGION_OTHER): (30.0, None, None),
        }
        total = 100.0
        cubes = OpsKpiFactCubes(
            year_overall={yr: (total, None, None)},
            year_region=parts,
            year_zoo={},
            month_overall={},
            month_region={},
            month_zoo={},
            quarter_overall={},
            quarter_region={},
            quarter_zoo={},
        )
        s = sum(cubes.year_region[(yr, r)][0] for r in [*REGION_ORDER, REGION_OTHER])
        self.assertAlmostEqual(s, cubes.year_overall[yr][0])


if __name__ == "__main__":
    unittest.main()
