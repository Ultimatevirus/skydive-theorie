import os
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import date
from unittest.mock import patch

import app as app_module
from app import app
from metar import (
    DUTCH_AIRPORTS,
    MetarError,
    _CACHE,
    get_daily_metar,
    grade_answers,
    metar_refresh_loop,
    parse_metar,
    refresh_daily_metars,
)


REPORT = "EHAM 021255Z 27012G20KT 9999 -RA SCT020 BKN035 12/08 Q1013"


def csrf_data(client, **data):
    with client.session_transaction() as session:
        token = session.get("csrf_token")
        if not token:
            token = "test-csrf-token"
            session["csrf_token"] = token
    return {"csrf_token": token, **data}


class MetarServiceTests(unittest.TestCase):
    def setUp(self):
        _CACHE.clear()
        self.database = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.database.close()
        self.previous_db_path = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = self.database.name

    def tearDown(self):
        _CACHE.clear()
        if self.previous_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self.previous_db_path
        os.remove(self.database.name)

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

    def test_parser_keeps_mean_and_variable_wind_directions(self):
        parsed = parse_metar(
            "EHRD 022125Z AUTO 23013KT 200V260 9999 BKN033/// OVC037/// 19/14 Q1018 NOSIG="
        )

        self.assertEqual(parsed["wind_direction"], "230")
        self.assertEqual(parsed["wind_speed"], 13.0)
        self.assertEqual(parsed["wind_direction_variation"], "200-260")
        self.assertEqual(parsed["clouds"], "BKN 033, OVC 037")
        self.assertEqual(parsed["weather"], "Geen significant weer")
        self.assertEqual(parsed["temperature"], 19.0)
        self.assertEqual(parsed["dew_point"], 14.0)
        self.assertEqual(parsed["qnh"], 1018.0)

        compound_weather = parse_metar("EHDR 022125Z 23013KT 9999 -DZRA 19/14 Q1018")
        self.assertEqual(compound_weather["weather"], "-DZRA")

    def test_knmi_bulletin_xml_is_reduced_to_human_readable_metar(self):
        content = (
            "0000358701\\r\\nLANL80 EHAM 022051\\r\\n"
            "<?xml version=\"1.0\"?><iwxxm:METAR>"
            "<!-- METAR EHAM 022055Z 20009KT 9999 -DZRA FEW024 SCT031 BKN040 18/15 Q1018 NOSIG= -->"
            "</iwxxm:METAR>"
        )

        with patch.dict(
            "os.environ",
            {"KNMI_API_KEY": "test-key", "KNMI_METAR_URL": "https://example.test/{airport}"},
        ):
            result = get_daily_metar("EHAM", date(2026, 9, 2), opener=self._json_opener(content))

        self.assertEqual(result["raw"], "EHAM 022055Z 20009KT 9999 -DZRA FEW024 SCT031 BKN040 18/15 Q1018 NOSIG=")

    @staticmethod
    def _json_opener(report):
        opener = unittest.mock.Mock()
        response = unittest.mock.MagicMock()
        response.read.return_value = json.dumps({"metar": report}).encode()
        response.__enter__.return_value = response
        opener.return_value = response
        return opener

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

        conn = sqlite3.connect(self.database.name)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT datetime, airport, metar FROM metar"
                ).fetchone(),
                ("2026-09-02", "EHAM", REPORT),
            )
        finally:
            conn.close()

    def test_cached_xml_is_normalised_and_rewritten(self):
        conn = sqlite3.connect(self.database.name)
        conn.execute("CREATE TABLE metar (datetime TEXT, airport TEXT, metar TEXT)")
        conn.execute(
            "INSERT INTO metar VALUES (?, ?, ?)",
            ("2026-09-02", "EHRD", f"<METAR><raw_text>{REPORT.replace('EHAM', 'EHRD')}</raw_text></METAR>"),
        )
        conn.commit()
        conn.close()

        result = get_daily_metar("EHRD", date(2026, 9, 2), opener=unittest.mock.Mock())

        self.assertEqual(result["raw"], REPORT.replace("EHAM", "EHRD"))
        conn = sqlite3.connect(self.database.name)
        try:
            self.assertEqual(conn.execute("SELECT metar FROM metar").fetchone()[0], result["raw"])
        finally:
            conn.close()

    def test_open_data_api_lists_and_downloads_xml_metar(self):
        responses = []
        for body in (
            b'{"files": [{"filename": "A_LANL80EHAM022022_C_EHAM_020926202202.xml"}]}',
            b'{"temporaryDownloadUrl": "https://download.example/metar.xml"}',
            b'<METAR><raw_text>EHAM 021255Z 27012G20KT 9999 -RA SCT020 BKN035 12/08 Q1013</raw_text></METAR>',
        ):
            response = unittest.mock.MagicMock()
            response.read.return_value = body
            response.__enter__.return_value = response
            responses.append(response)
        opener = unittest.mock.Mock(side_effect=responses)

        with patch.dict("os.environ", {"KNMI_API_KEY": "test-key", "KNMI_METAR_URL": "", "KNMI_OPEN_DATA_URL": "https://example.test/open-data"}):
            result = get_daily_metar("EHAM", date(2026, 9, 2), opener=opener)

        self.assertEqual(result["raw"], REPORT)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(opener.call_args_list[0].args[0].headers["Authorization"], "test-key")

    def test_refresh_fetches_each_airport_once_and_continues_after_failure(self):
        calls = []

        def fetch(code, day, opener):
            calls.append(code)
            if code == "EHRD":
                raise MetarError("temporary")
            return {}

        with patch("metar.get_daily_metar", side_effect=fetch):
            refresh_daily_metars(date(2026, 9, 3))

        self.assertEqual(calls, list(dict.fromkeys(DUTCH_AIRPORTS)))
        self.assertEqual(calls.count("EHAM"), 1)

    def test_refresh_loop_waits_30_minutes_between_refreshes(self):
        stop_event = unittest.mock.Mock()
        stop_event.is_set.side_effect = [False, True]
        wait_calls = []
        stop_event.wait.side_effect = lambda seconds: wait_calls.append(seconds)

        with patch("metar.refresh_daily_metars") as refresh_metars:
            metar_refresh_loop(stop_event=stop_event, opener=unittest.mock.Mock())

        self.assertEqual(refresh_metars.call_count, 1)
        self.assertEqual(wait_calls, [1800])

    def test_foreground_fetch_returns_busy_error_when_another_worker_holds_claim(self):
        conn = sqlite3.connect(self.database.name)
        conn.execute(
            "CREATE TABLE metar_fetch_claim (datetime TEXT, airport TEXT, claimed_at REAL, PRIMARY KEY (datetime, airport))"
        )
        conn.execute(
            "INSERT INTO metar_fetch_claim VALUES (?, ?, ?)",
            ("2026-09-02", "EHAM", time.time()),
        )
        conn.commit()
        conn.close()

        with patch.dict("os.environ", {"KNMI_API_KEY": "test-key", "KNMI_METAR_URL": "https://example.test/{airport}"}):
            with self.assertRaises(MetarError) as context:
                get_daily_metar("EHAM", date(2026, 9, 2), opener=unittest.mock.Mock(), wait_for_rate_limit=False)

        self.assertEqual(str(context.exception), "Er wordt al een METAR opgehaald. Probeer het zo opnieuw.")

    def test_grading_allows_small_numeric_difference(self):
        parsed = parse_metar(REPORT)
        result = grade_answers(
            parsed,
            {
                "wind_direction": "270",
                "wind_direction_variation": "",
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

    def test_grading_accepts_empty_weather_and_missing_gust(self):
        parsed = parse_metar("EHRD 022125Z 23013KT 9999 BKN033 19/14 Q1018")

        result = grade_answers(
            parsed,
            {
                "wind_direction": "230",
                "wind_speed": "13",
                "wind_direction_variation": "",
                "visibility": "9999",
                "temperature": "19",
                "dew_point": "14",
                "qnh": "1018",
                "clouds": "BKN 033",
                "weather": "",
                "wind_gust": "",
            },
        )

        self.assertTrue(all(item["correct"] for item in result))

    def test_grading_accepts_v_notation_for_wind_variation(self):
        parsed = parse_metar("EHRD 022125Z 23013KT 200V260 9999 19/14 Q1018")
        result = grade_answers(
            parsed,
            {"wind_direction": "230", "wind_direction_variation": "200V260", "wind_speed": "13"},
        )

        self.assertTrue(next(item for item in result if item["field"] == "wind_direction_variation")["correct"])


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
                data=csrf_data(
                    client,
                    airport="EHAM",
                    action="check",
                    wind_direction="270",
                    wind_speed="12",
                    wind_gust="20",
                    visibility="9999",
                    temperature="12",
                    dew_point="8",
                    qnh="1013",
                    weather="-RA",
                    clouds="SCT 020, BKN 035",
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("10 / 10", response.get_data(as_text=True))

    def test_loading_another_airport_uses_that_selection(self):
        eham = parse_metar(REPORT)
        eham.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        ehrd = parse_metar(REPORT.replace("EHAM", "EHRD"))
        ehrd.update({"airport": "EHRD", "airport_name": DUTCH_AIRPORTS["EHRD"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", side_effect=[eham, ehrd]) as get_metar:
            client.post("/metar", data=csrf_data(client, airport="EHAM", action="load"))
            response = client.post("/metar", data=csrf_data(client, airport="EHRD", action="load"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("EHRD · Rotterdam The Hague", response.get_data(as_text=True))
        self.assertEqual(get_metar.call_args_list[1].args[0], "EHRD")

    def test_invalid_airport_is_rejected(self):
        response = app.test_client().get("/metar?airport=XXXX")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Kies een Nederlandse luchthaven", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
