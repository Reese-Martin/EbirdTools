from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from ebird_tools.ebird_api import fetch_ebird_taxonomy, fetch_recent_observations


class EbirdAPITests(unittest.TestCase):
    @patch("ebird_tools.ebird_api.requests.get")
    def test_taxonomy_is_normalized_without_an_api_key(self, get: Mock) -> None:
        response = Mock()
        response.json.return_value = [
            {
                "sciName": "Turdus migratorius",
                "comName": "American Robin",
                "speciesCode": "amerob",
                "category": "species",
                "order": "Passeriformes",
                "familyComName": "Thrushes and Allies",
                "familySciName": "Turdidae",
                "bandingCodes": ["AMRO"],
            }
        ]
        get.return_value = response

        result = fetch_ebird_taxonomy()

        self.assertEqual(result.iloc[0]["Species Code"], "amerob")
        self.assertEqual(result.iloc[0]["Banding Codes"], "AMRO")
        request = get.call_args
        self.assertNotIn("headers", request.kwargs)
        self.assertEqual(request.kwargs["params"], {"fmt": "json", "locale": "en"})

    @patch("ebird_tools.ebird_api.requests.get")
    def test_recent_observations_are_normalized(self, get: Mock) -> None:
        response = Mock()
        response.json.return_value = [
            {
                "comName": "Blue Jay",
                "sciName": "Cyanocitta cristata",
                "locName": "Example Park",
                "obsDt": "2026-08-20 09:30",
                "howMany": 2,
                "lat": 34.0,
                "lng": -118.0,
                "subId": "S123",
            }
        ]
        get.return_value = response

        result = fetch_recent_observations("secret", "US-CA", back=14)

        self.assertEqual(result.iloc[0]["Common Name"], "Blue Jay")
        self.assertEqual(result.iloc[0]["Scientific Name"], "Cyanocitta cristata")
        request = get.call_args
        self.assertEqual(request.kwargs["headers"]["X-eBirdApiToken"], "secret")
        self.assertEqual(request.kwargs["params"]["back"], 14)


if __name__ == "__main__":
    unittest.main()
