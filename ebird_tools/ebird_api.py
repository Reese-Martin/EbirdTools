"""Small, optional client for public eBird recent-observation data."""

from __future__ import annotations

import pandas as pd
import requests


class EbirdAPIError(RuntimeError):
    """Raised when the public eBird API cannot fulfill a request."""


def fetch_ebird_taxonomy(locale: str = "en", timeout: int = 60) -> pd.DataFrame:
    """Fetch public eBird taxonomy metadata without requiring an API key."""

    try:
        response = requests.get(
            "https://api.ebird.org/v2/ref/taxonomy/ebird",
            params={"fmt": "json", "locale": locale},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise EbirdAPIError(
            f"eBird taxonomy returned HTTP {status}. Try again later."
        ) from exc
    except (requests.RequestException, ValueError) as exc:
        raise EbirdAPIError(f"Could not retrieve the eBird taxonomy: {exc}") from exc

    if not isinstance(payload, list):
        raise EbirdAPIError("eBird returned an unexpected taxonomy format.")

    desired = {
        "sciName": "Scientific Name",
        "comName": "Common Name",
        "speciesCode": "Species Code",
        "category": "Category",
        "taxonOrder": "Taxonomic Order",
        "order": "Order",
        "familyCode": "Family Code",
        "familyComName": "Family Common Name",
        "familySciName": "Family Scientific Name",
        "bandingCodes": "Banding Codes",
        "comNameCodes": "Common Name Codes",
        "sciNameCodes": "Scientific Name Codes",
        "reportAs": "Report As",
    }
    taxonomy = pd.DataFrame(payload).rename(columns=desired)
    for column in desired.values():
        if column not in taxonomy:
            taxonomy[column] = pd.NA
    for column in ["Banding Codes", "Common Name Codes", "Scientific Name Codes"]:
        taxonomy[column] = taxonomy[column].map(
            lambda value: ", ".join(value) if isinstance(value, list) else value
        )
    return taxonomy[list(desired.values())]


def fetch_recent_observations(
    api_key: str,
    region_code: str,
    back: int = 14,
    max_results: int = 500,
    timeout: int = 30,
) -> pd.DataFrame:
    """Fetch recent regional observations without accessing personal history."""

    if not api_key.strip():
        raise EbirdAPIError(
            "Enter an eBird API key before requesting recent sightings."
        )
    if not region_code.strip():
        raise EbirdAPIError("Choose or enter an eBird region code.")
    back = max(1, min(int(back), 30))
    max_results = max(1, min(int(max_results), 10_000))
    url = f"https://api.ebird.org/v2/data/obs/{region_code.strip()}/recent"
    try:
        response = requests.get(
            url,
            headers={"X-eBirdApiToken": api_key.strip()},
            params={
                "back": back,
                "detail": "simple",
                "includeProvisional": "false",
                "maxResults": max_results,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise EbirdAPIError(
            f"eBird returned HTTP {status}. Check the API key and region."
        ) from exc
    except (requests.RequestException, ValueError) as exc:
        raise EbirdAPIError(
            f"Could not retrieve recent eBird observations: {exc}"
        ) from exc

    if not isinstance(payload, list):
        raise EbirdAPIError("eBird returned an unexpected response format.")
    if not payload:
        return pd.DataFrame(
            columns=[
                "Common Name",
                "Scientific Name",
                "Location",
                "Observed At",
                "Count",
                "Latitude",
                "Longitude",
                "Checklist ID",
            ]
        )
    result = pd.DataFrame(payload).rename(
        columns={
            "comName": "Common Name",
            "sciName": "Scientific Name",
            "locName": "Location",
            "obsDt": "Observed At",
            "howMany": "Count",
            "lat": "Latitude",
            "lng": "Longitude",
            "subId": "Checklist ID",
        }
    )
    desired = [
        "Common Name",
        "Scientific Name",
        "Location",
        "Observed At",
        "Count",
        "Latitude",
        "Longitude",
        "Checklist ID",
    ]
    for column in desired:
        if column not in result:
            result[column] = pd.NA
    result["Observed At"] = pd.to_datetime(result["Observed At"], errors="coerce")
    return result[desired].sort_values(
        ["Observed At", "Common Name"], ascending=[False, True], na_position="last"
    )
