from __future__ import annotations

import unittest

import pandas as pd

try:
    from ebird_tools.boundaries import (
        BoundaryBundle,
        assign_locations_to_boundaries,
        boundary_catalog,
        subset_boundary_bundle,
    )
except ModuleNotFoundError:
    BoundaryBundle = None


@unittest.skipIf(
    BoundaryBundle is None, "optional geospatial dependencies are unavailable"
)
class BoundaryTests(unittest.TestCase):
    def test_location_assignment_uses_polygon_geometry(self) -> None:
        bundle = BoundaryBundle(
            country_code="US",
            admin_level="ADM2",
            metadata={"boundaryID": "test"},
            geojson={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "west",
                        "properties": {
                            "_boundary_id": "west",
                            "_boundary_name": "West County",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[-2, -1], [0, -1], [0, 1], [-2, 1], [-2, -1]]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "id": "east",
                        "properties": {
                            "_boundary_id": "east",
                            "_boundary_name": "East County",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0, -1], [2, -1], [2, 1], [0, 1], [0, -1]]
                            ],
                        },
                    },
                ],
            },
        )
        locations = pd.DataFrame(
            {
                "Location ID": ["L1", "L2", "ocean"],
                "Latitude": [0.0, 0.0, 4.0],
                "Longitude": [-1.0, 1.0, 4.0],
            }
        )
        assignments = assign_locations_to_boundaries(locations, bundle)
        result = assignments.set_index("Location ID")["Boundary Name"].to_dict()
        self.assertEqual(result, {"L1": "West County", "L2": "East County"})
        self.assertEqual(len(boundary_catalog(bundle)), 2)

    def test_unique_county_name_is_an_offshore_fallback(self) -> None:
        bundle = BoundaryBundle(
            country_code="US",
            admin_level="ADM2",
            metadata={"boundaryID": "test"},
            geojson={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "island",
                        "properties": {
                            "_boundary_id": "island",
                            "_boundary_name": "Island County",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[-2, -1], [0, -1], [0, 1], [-2, 1], [-2, -1]]
                            ],
                        },
                    }
                ],
            },
        )
        locations = pd.DataFrame(
            {
                "Location ID": ["offshore"],
                "Latitude": [5.0],
                "Longitude": [5.0],
                "County": ["Island"],
            }
        )
        assignments = assign_locations_to_boundaries(locations, bundle)
        self.assertEqual(assignments.iloc[0]["Boundary Name"], "Island County")

    def test_subdivision_code_is_an_offshore_state_fallback(self) -> None:
        bundle = BoundaryBundle(
            country_code="US",
            admin_level="ADM1",
            metadata={"boundaryID": "test"},
            geojson={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "california",
                        "properties": {
                            "_boundary_id": "california",
                            "_boundary_name": "California",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[-2, -1], [0, -1], [0, 1], [-2, 1], [-2, -1]]
                            ],
                        },
                    }
                ],
            },
        )
        locations = pd.DataFrame(
            {
                "Location ID": ["pelagic"],
                "Latitude": [5.0],
                "Longitude": [5.0],
                "State/Province": ["US-CA"],
            }
        )

        assignments = assign_locations_to_boundaries(locations, bundle)

        self.assertEqual(assignments.iloc[0]["Boundary Name"], "California")

    def test_child_boundaries_can_be_subset_to_a_parent(self) -> None:
        parent_bundle = BoundaryBundle(
            country_code="US",
            admin_level="ADM1",
            metadata={"boundaryID": "parents"},
            geojson={
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "west-state",
                        "properties": {
                            "_boundary_id": "west-state",
                            "_boundary_name": "West State",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[-2, -2], [0, -2], [0, 2], [-2, 2], [-2, -2]]
                            ],
                        },
                    },
                    {
                        "type": "Feature",
                        "id": "east-state",
                        "properties": {
                            "_boundary_id": "east-state",
                            "_boundary_name": "East State",
                        },
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [[0, -2], [2, -2], [2, 2], [0, 2], [0, -2]]
                            ],
                        },
                    },
                ],
            },
        )
        child_features = []
        for child_id, left, right in [
            ("west-one", -2, -1),
            ("west-two", -1, 0),
            ("east-one", 0, 2),
        ]:
            child_features.append(
                {
                    "type": "Feature",
                    "id": child_id,
                    "properties": {
                        "_boundary_id": child_id,
                        "_boundary_name": child_id,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[left, -1], [right, -1], [right, 1], [left, 1], [left, -1]]
                        ],
                    },
                }
            )
        child_bundle = BoundaryBundle(
            country_code="US",
            admin_level="ADM2",
            metadata={"boundaryID": "children"},
            geojson={"type": "FeatureCollection", "features": child_features},
        )

        subset = subset_boundary_bundle(child_bundle, parent_bundle, "west-state")

        self.assertEqual(
            {feature["id"] for feature in subset.geojson["features"]},
            {"west-one", "west-two"},
        )
        self.assertEqual(subset.metadata["parentBoundaryID"], "west-state")


if __name__ == "__main__":
    unittest.main()
