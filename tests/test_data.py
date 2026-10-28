from __future__ import annotations

import unittest
from io import StringIO

import pandas as pd

from ebird_tools.data import (
    DataValidationError,
    build_annual_accumulation,
    build_checklists,
    build_chronotype,
    build_location_summary,
    build_species_summary,
    load_and_prepare_csv,
)

CSV = """Submission ID,Common Name,Scientific Name,Taxonomic Order,Count,State/Province,County,Location ID,Location,Latitude,Longitude,Date,Time,Protocol,Duration (Min),All Obs Reported,Distance Traveled (km),Area Covered (ha),Number of Observers,Breeding Code,Observation Details,Checklist Comments,ML Catalog Numbers
S1,American Robin,Turdus migratorius,1,X,US-CA,Example,LZ,Zeta Marsh,34.0,-118.0,2023-01-01,06:00 AM,eBird - Traveling Count,30,1,1.0,,1,,,,
S1,Blue Jay,Cyanocitta cristata,2,2,US-CA,Example,LZ,Zeta Marsh,34.0,-118.0,2023-01-01,06:00 AM,eBird - Traveling Count,30,1,1.0,,1,,,,
S2,American Robin,Turdus migratorius,1,3,US-CA,Example,LA,Alpha Park,34.1,-118.1,2024-02-29,02:00 PM,eBird - Stationary Count,20,1,,,1,,,,
S2,Blue Jay,Cyanocitta cristata,2,1,US-CA,Example,LA,Alpha Park,34.1,-118.1,2024-02-29,02:00 PM,eBird - Stationary Count,20,1,,,1,,,,
S3,Blue Jay,Cyanocitta cristata,2,4,US-CA,Example,LB,Beta Lake,34.2,-118.2,2024-12-31,06:00 AM,eBird - Casual Observation,,0,,,1,,,,
"""


class DataPreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.observations = load_and_prepare_csv(StringIO(CSV)).observations
        self.checklists = build_checklists(self.observations)

    def test_presence_only_x_is_retained(self) -> None:
        robin = self.observations.loc[
            self.observations["Common Name"] == "American Robin"
        ]
        self.assertEqual(len(robin), 2)
        self.assertEqual(int(robin["Count Is Presence Only"].sum()), 1)
        self.assertEqual(int(robin["Count Numeric"].notna().sum()), 1)

    def test_first_sighting_keeps_associated_location(self) -> None:
        summary = build_species_summary(
            self.observations, reference_date=pd.Timestamp("2025-01-01")
        )
        robin = summary.loc[summary["Common Name"] == "American Robin"].iloc[0]
        self.assertEqual(robin["First Location"], "Zeta Marsh")
        self.assertEqual(robin["First Seen"], pd.Timestamp("2023-01-01"))
        self.assertEqual(robin["Checklist Count"], 2)
        self.assertEqual(robin["Exact Individuals"], 3)

    def test_year_filtered_location_visits_share_the_filter(self) -> None:
        locations = build_location_summary(
            self.observations, self.checklists, years=[2024]
        )
        self.assertEqual(int(locations["Location Visits"].sum()), 2)
        self.assertEqual(set(locations["Location ID"]), {"LA", "LB"})

    def test_chronotype_is_checklist_level_and_hour_aligned(self) -> None:
        chronotype = build_chronotype(self.observations, self.checklists)
        six = chronotype.loc[chronotype["Hour"] == 6].iloc[0]
        fourteen = chronotype.loc[chronotype["Hour"] == 14].iloc[0]
        self.assertEqual(int(chronotype["Checklists"].sum()), 3)
        self.assertEqual(six["Checklists"], 2)
        self.assertEqual(six["Average Species Per Checklist"], 1.5)
        self.assertEqual(fourteen["Checklists"], 1)
        self.assertEqual(fourteen["Average Species Per Checklist"], 2.0)

    def test_annual_accumulation_is_leap_safe(self) -> None:
        annual = build_annual_accumulation(self.observations)
        final_2024 = annual.loc[annual["Year"] == 2024].iloc[-1]
        self.assertEqual(final_2024["Calendar Day"], 366)
        self.assertLess(final_2024["Theta"], 360)

    def test_missing_required_columns_are_reported(self) -> None:
        with self.assertRaises(DataValidationError):
            load_and_prepare_csv(StringIO("Date,Common Name\n2024-01-01,Blue Jay\n"))


if __name__ == "__main__":
    unittest.main()
