"""Administrative-boundary retrieval and point-in-polygon assignment."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass

import pandas as pd
import pycountry
import requests
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

GEBOUNDARIES_API = "https://www.geoboundaries.org/api/current/gbOpen"


class BoundaryError(RuntimeError):
    """Raised when boundary data cannot be retrieved or interpreted."""


@dataclass(frozen=True)
class BoundaryBundle:
    """A normalized GeoJSON boundary layer and its source metadata."""

    country_code: str
    admin_level: str
    geojson: dict
    metadata: dict

    @property
    def cache_key(self) -> str:
        return str(
            self.metadata.get("boundaryID")
            or self.metadata.get("buildUpdateDate")
            or f"{self.country_code}-{self.admin_level}"
        )

    @property
    def attribution(self) -> str:
        source = self.metadata.get("boundarySource") or "geoBoundaries"
        license_name = self.metadata.get("boundaryLicense") or "see source metadata"
        year = self.metadata.get("boundaryYearRepresented") or "unknown vintage"
        return f"Boundaries: {source}; {year}; {license_name}."


def _iso3(country_code: str) -> str:
    country = pycountry.countries.get(alpha_2=country_code.upper())
    if country is None:
        raise BoundaryError(f"Unknown two-letter country code: {country_code}")
    return country.alpha_3


def _normalize_geojson(geojson: dict) -> dict:
    if geojson.get("type") != "FeatureCollection":
        raise BoundaryError("The boundary provider returned an invalid GeoJSON layer.")

    normalized = deepcopy(geojson)
    seen_ids: set[str] = set()
    features: list[dict] = []
    for index, feature in enumerate(normalized.get("features", [])):
        if not feature.get("geometry"):
            continue
        properties = feature.setdefault("properties", {})
        feature_id = (
            feature.get("id")
            or properties.get("shapeID")
            or properties.get("boundaryID")
            or properties.get("GID_2")
            or properties.get("GID_1")
            or f"boundary-{index}"
        )
        feature_id = str(feature_id)
        if feature_id in seen_ids:
            feature_id = f"{feature_id}-{index}"
        seen_ids.add(feature_id)
        feature["id"] = feature_id
        properties["_boundary_id"] = feature_id
        properties["_boundary_name"] = str(
            properties.get("shapeName")
            or properties.get("NAME_2")
            or properties.get("NAME_1")
            or properties.get("name")
            or feature_id
        )
        features.append(feature)
    if not features:
        raise BoundaryError("The boundary layer contained no usable polygons.")
    normalized["features"] = features
    return normalized


def fetch_boundary_bundle(
    country_code: str,
    preferred_levels: tuple[str, ...] = ("ADM2", "ADM1"),
    timeout: int = 30,
) -> BoundaryBundle:
    """Fetch the best available simplified administrative layer for a country."""

    iso3 = _iso3(country_code)
    errors: list[str] = []
    for admin_level in preferred_levels:
        metadata_url = f"{GEBOUNDARIES_API}/{iso3}/{admin_level}/"
        try:
            metadata_response = requests.get(metadata_url, timeout=timeout)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            if isinstance(metadata, list):
                metadata = metadata[0] if metadata else {}
            geometry_url = metadata.get("simplifiedGeometryGeoJSON") or metadata.get(
                "gjDownloadURL"
            )
            if not geometry_url:
                raise BoundaryError("No GeoJSON download URL was provided.")
            geometry_response = requests.get(geometry_url, timeout=timeout)
            geometry_response.raise_for_status()
            geojson = _normalize_geojson(geometry_response.json())
            return BoundaryBundle(
                country_code=country_code.upper(),
                admin_level=admin_level,
                geojson=geojson,
                metadata=metadata,
            )
        except (requests.RequestException, ValueError, BoundaryError) as exc:
            errors.append(f"{admin_level}: {exc}")
    raise BoundaryError(
        f"No county-like boundary layer was available for {country_code.upper()}. "
        + " | ".join(errors)
    )


def boundary_catalog(bundle: BoundaryBundle) -> pd.DataFrame:
    """Return identifiers and display names for every polygon in a bundle."""

    return pd.DataFrame(
        [
            {
                "Boundary ID": feature["id"],
                "Boundary Name": feature["properties"]["_boundary_name"],
            }
            for feature in bundle.geojson["features"]
        ]
    )


def subset_boundary_bundle(
    child_bundle: BoundaryBundle,
    parent_bundle: BoundaryBundle,
    parent_boundary_id: str,
) -> BoundaryBundle:
    """Return child polygons geographically contained by a parent polygon."""

    if child_bundle.country_code != parent_bundle.country_code:
        raise BoundaryError("Parent and child boundary layers must be for one country.")

    parent_feature = next(
        (
            feature
            for feature in parent_bundle.geojson["features"]
            if str(feature["id"]) == str(parent_boundary_id)
        ),
        None,
    )
    if parent_feature is None:
        raise BoundaryError(f"Parent boundary {parent_boundary_id!r} was not found.")

    try:
        parent_geometry = shape(parent_feature["geometry"])
        if not parent_geometry.is_valid:
            parent_geometry = parent_geometry.buffer(0)
    except (TypeError, ValueError) as exc:
        raise BoundaryError(
            "The selected parent boundary has invalid geometry."
        ) from exc

    selected_features: list[dict] = []
    for feature in child_bundle.geojson["features"]:
        try:
            child_geometry = shape(feature["geometry"])
            if not child_geometry.is_valid:
                child_geometry = child_geometry.buffer(0)
            if child_geometry.is_empty:
                continue
        except (TypeError, ValueError):
            continue
        if parent_geometry.covers(child_geometry.representative_point()):
            selected_features.append(deepcopy(feature))

    if not selected_features:
        raise BoundaryError(
            "No child boundaries were found inside the selected parent boundary."
        )

    geojson = deepcopy(child_bundle.geojson)
    geojson["features"] = selected_features
    metadata = deepcopy(child_bundle.metadata)
    metadata["parentBoundaryID"] = str(parent_boundary_id)
    return BoundaryBundle(
        country_code=child_bundle.country_code,
        admin_level=child_bundle.admin_level,
        geojson=geojson,
        metadata=metadata,
    )


def _normalized_boundary_name(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    text = re.sub(
        r"\b(county|parish|borough|municipality|district|census area)\b",
        "",
        text.lower(),
    )
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _fallback_boundary_name(row: pd.Series, bundle: BoundaryBundle) -> str | None:
    if bundle.admin_level == "ADM1" and pd.notna(row.get("State/Province")):
        subdivision = pycountry.subdivisions.get(code=str(row["State/Province"]))
        if subdivision is not None:
            return subdivision.name
    if bundle.admin_level == "ADM2" and pd.notna(row.get("County")):
        return str(row["County"])
    return None


def assign_locations_to_boundaries(
    locations: pd.DataFrame, bundle: BoundaryBundle
) -> pd.DataFrame:
    """Assign unique eBird locations to polygons using exact coordinates."""

    required = {"Location ID", "Latitude", "Longitude"}
    missing = required.difference(locations.columns)
    if missing:
        raise BoundaryError(
            "Location assignment is missing columns: " + ", ".join(sorted(missing))
        )

    feature_records: list[tuple[str, str]] = []
    geometries = []
    for feature in bundle.geojson["features"]:
        try:
            geometry = shape(feature["geometry"])
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            if geometry.is_empty:
                continue
        except (TypeError, ValueError):
            continue
        geometries.append(geometry)
        feature_records.append(
            (
                str(feature["id"]),
                str(feature["properties"]["_boundary_name"]),
            )
        )
    if not geometries:
        raise BoundaryError("The boundary layer contained no valid polygon geometry.")

    tree = STRtree(geometries)
    boundary_name_ids: dict[str, list[int]] = {}
    for index, (_, boundary_name) in enumerate(feature_records):
        boundary_name_ids.setdefault(
            _normalized_boundary_name(boundary_name), []
        ).append(index)

    assignments: list[dict] = []
    unique_locations = locations.drop_duplicates("Location ID")
    for _, row in unique_locations.iterrows():
        location_id = str(row["Location ID"])
        if pd.isna(row["Latitude"]) or pd.isna(row["Longitude"]):
            continue
        point = Point(float(row["Longitude"]), float(row["Latitude"]))
        for candidate in tree.query(point):
            candidate_index = int(candidate)
            if geometries[candidate_index].covers(point):
                boundary_id, boundary_name = feature_records[candidate_index]
                assignments.append(
                    {
                        "Location ID": location_id,
                        "Boundary ID": boundary_id,
                        "Boundary Name": boundary_name,
                    }
                )
                break
        else:
            # Offshore and simplified-coastline points can miss an otherwise valid
            # polygon. eBird's state/county fields are safe fallbacks only when a
            # normalized name identifies exactly one polygon in the country.
            fallback_name = _fallback_boundary_name(row, bundle)
            if fallback_name is not None:
                matches = boundary_name_ids.get(
                    _normalized_boundary_name(fallback_name), []
                )
                if len(matches) == 1:
                    boundary_id, boundary_name = feature_records[matches[0]]
                    assignments.append(
                        {
                            "Location ID": location_id,
                            "Boundary ID": boundary_id,
                            "Boundary Name": boundary_name,
                        }
                    )
    return pd.DataFrame(
        assignments, columns=["Location ID", "Boundary ID", "Boundary Name"]
    ).drop_duplicates("Location ID")
