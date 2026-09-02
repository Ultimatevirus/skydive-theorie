import unittest
from datetime import date
from unittest.mock import patch

import app as app_module
from app import app
from metar import DUTCH_AIRPORTS, _CACHE, get_daily_metar, grade_answers, parse_metar


REPORT = "EHAM 021255Z 27012G20KT 9999 -RA SCT020 BKN035 12/08 Q1013"


class MetarServiceTests(unittest.TestCase):
    def setUp(self):
        _CACHE.clear()

    def test_parser_decodes_common_groups(self):
        parsed = parse_metar(REPORT)

        self.assertEqual(parsed["wind_direction"], "270")
        self.assertEqual(parsed["wind_speed"], 12.0)
        self.assertEqual(parsed["wind_gust"], 20.0)
        self.assertEqual(parsed["visibility"], 9999.0)
        self.assertEqual(parsed["temperature"], 12.0)
        self.assertEqual(parsed["dew_point"], 8.0)
        self.assertEqual(parsed["qnh"], 1013.0)
        self.assertEqual(parsed["weather"], "-RA")

    def test_daily_fetch_is_cached_per_airport_and_date(self):
        opener = unittest.mock.Mock()
        response = unittest.mock.MagicMock()
        response.read.return_value = b'{"metar": "' + REPORT.encode() + b'"}'
        response.__enter__.return_value = response
        opener.return_value = response

        with patch.dict("os.environ", {"KNMI_API_KEY": "test-key", "KNMI_METAR_URL": "https://example.test/{airport}"}):
            first = get_daily_metar("EHAM", date(2026, 9, 2), opener=opener)
            second = get_daily_metar("EHAM", date(2026, 9, 2), opener=opener)

        self.assertEqual(first, second)
        opener.assert_called_once()

    def test_grading_allows_small_numeric_difference(self):
        parsed = parse_metar(REPORT)
        result = grade_answers(
            parsed,
            {
                "wind_direction": "270",
                "wind_speed": "13",
                "wind_gust": "19",
                "visibility": "10000",
                "temperature": "12",
                "dew_point": "8",
                "qnh": "1014",
                "weather": "-RA",
                "clouds": "SCT 020, BKN 035",
            },
        )

        self.assertTrue(all(item["correct"] for item in result))


class MetarRouteTests(unittest.TestCase):
    def test_get_and_post_metar_page(self):
        parsed = parse_metar(REPORT)
        parsed.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", return_value=parsed):
            response = client.get("/metar?airport=EHAM")
            self.assertEqual(response.status_code, 200)
            self.assertIn(REPORT, response.get_data(as_text=True))

            response = client.post(
                "/metar",
                data={
                    "airport": "EHAM",
                    "wind_direction": "270",
                    "wind_speed": "12",
                    "wind_gust": "20",
                    "visibility": "9999",
                    "temperature": "12",
                    "dew_point": "8",
                    "qnh": "1013",
                    "weather": "-RA",
                    "clouds": "SCT 020, BKN 035",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("9 / 9", response.get_data(as_text=True))

    def test_invalid_airport_is_rejected(self):
        response = app.test_client().get("/metar?airport=XXXX")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Kies een Nederlandse luchthaven", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
