import os
import json
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import taf as taf_module
from db import DEFAULT_LOCAL_DB
from taf import (
    TafError,
    _CACHE,
    build_abbreviation_questions,
    build_questions,
    get_daily_taf,
    grade_answers,
    load_weather_definitions,
    parse_taf,
    refresh_daily_tafs,
    taf_refresh_loop,
)


_access_gate_patcher = patch.dict(os.environ, {"ACCESS_GATE_ENABLED": "0"})


def setUpModule():
    _access_gate_patcher.start()


def tearDownModule():
    _access_gate_patcher.stop()


REPORT = (
    "TAF EHAM 121100Z 1212/1318 24010G20KT 9999 SCT025 "
    "BECMG 1214/1216 27015KT TEMPO 1215/1218 4000 -SHRA BKN012CB "
    "PROB30 TEMPO 1315/1318 TSRA FM130600 30012KT 9999 NSW BKN020"
)
DEFINITIONS = load_weather_definitions(DEFAULT_LOCAL_DB)


class FakeResponse:
    def __init__(self, body):
        self.body = body.encode("utf-8") if isinstance(body, str) else body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class TafTestCase(unittest.TestCase):
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


class TafParserTests(unittest.TestCase):
    def test_header_and_base_conditions(self):
        parsed = parse_taf(REPORT)

        self.assertEqual(parsed["airport"], "EHAM")
        self.assertEqual(parsed["issued_time"], "11:00")
        self.assertEqual(parsed["valid_period"], "1212/1318")
        self.assertEqual(parsed["valid_hours"], 30)
        self.assertEqual(parsed["wind_direction"], "240")
        self.assertEqual(parsed["wind_speed"], 10.0)
        self.assertEqual(parsed["wind_gust"], 20.0)
        self.assertEqual(parsed["visibility"], 9999.0)
        self.assertEqual(parsed["clouds"], "SCT 025")

    def test_change_groups_are_split_and_formatted_on_separate_lines(self):
        parsed = parse_taf(REPORT)

        self.assertEqual(
            [change["type"] for change in parsed["changes"]],
            ["BECMG", "TEMPO", "PROB30 TEMPO", "FM"],
        )
        self.assertEqual(parsed["changes"][0]["period"], "1214/1216")
        self.assertEqual(parsed["changes"][0]["wind_speed"], 15.0)
        self.assertEqual(parsed["changes"][1]["weather"], "-SHRA")
        self.assertEqual(parsed["changes"][3]["period"], "FM130600")
        self.assertTrue(parsed["changes"][3]["nsw"])
        self.assertEqual(len(parsed["lines"]), 5)
        self.assertTrue(parsed["lines"][0].startswith("TAF EHAM 121100Z"))
        self.assertEqual(parsed["lines"][2], "TEMPO 1215/1218 4000 -SHRA BKN012CB")

    def test_prob40_without_tempo_and_cavok_base(self):
        parsed = parse_taf("TAF EHRD 121100Z 1212/1318 CAVOK PROB40 1318/1322 4000 -SHRA=")

        self.assertEqual(parsed["clouds"], "CAVOK")
        self.assertEqual(parsed["changes"][0]["type"], "PROB40")
        self.assertEqual(parsed["changes"][0]["visibility"], 4000.0)

    def test_temperature_groups_do_not_leak_into_change_conditions(self):
        parsed = parse_taf("TAF EHAM 121100Z 1212/1318 24010KT 9999 FEW020 TX12/1315Z TN05/1303Z")

        self.assertEqual(parsed["changes"], [])
        self.assertEqual(parsed["clouds"], "FEW 020")

    def test_extract_taf_from_bulletin(self):
        content = b"<!-- TAF EHAM 121100Z 1212/1318 24010KT\n 9999 SCT025= --><x/>"

        self.assertEqual(
            taf_module._extract_taf(content, "EHAM"),
            "TAF EHAM 121100Z 1212/1318 24010KT 9999 SCT025",
        )
        self.assertIsNone(taf_module._extract_taf(content, "EHRD"))


class TafServiceTests(TafTestCase):
    def test_daily_fetch_is_cached_per_airport_and_date(self):
        calls = []

        def fake_fetch(code, day, api_key, opener):
            calls.append(code)
            return REPORT

        with patch.dict(os.environ, {"KNMI_API_KEY": "key"}), \
                patch.object(taf_module, "_get_open_data_taf", fake_fetch):
            first = get_daily_taf("EHAM", date(2026, 10, 12))
            _CACHE.clear()
            second = get_daily_taf("eham", date(2026, 10, 12))

        self.assertEqual(calls, ["EHAM"])
        self.assertEqual(first["raw"], second["raw"])
        self.assertEqual(second["airport_name"], "Amsterdam Schiphol")

    def test_invalid_airport_and_cache_only(self):
        with self.assertRaises(TafError):
            get_daily_taf("EGLL", date(2026, 10, 12))
        with self.assertRaises(TafError):
            get_daily_taf("EHAM", date(2026, 10, 12), cache_only=True)

    def test_missing_api_key_releases_claim(self):
        with patch.dict(os.environ, {"KNMI_API_KEY": ""}):
            with self.assertRaises(TafError):
                get_daily_taf("EHAM", date(2026, 10, 12))
            self.assertTrue(taf_module._claim_taf_fetch(date(2026, 10, 12), "EHAM", wait=False))

    def test_busy_claim_returns_error_without_fetching(self):
        self.assertTrue(taf_module._claim_taf_fetch(date(2026, 10, 12), "EHAM", wait=False))
        with self.assertRaises(TafError):
            get_daily_taf("EHAM", date(2026, 10, 12), wait_for_rate_limit=False)

    def test_open_data_api_lists_and_downloads_taf(self):
        urls = []

        def opener(request, timeout):
            url = request.full_url
            urls.append(url)
            if "/datasets/taf/versions/1.0/files?" in url:
                return FakeResponse(json.dumps({"files": [{"filename": "TAF_EHAM_202610121100.xml"}]}))
            if url.endswith("/url"):
                return FakeResponse(json.dumps({"temporaryDownloadUrl": "https://download.example/taf"}))
            return FakeResponse("<!-- TAF EHAM 121100Z 1212/1318 24010KT 9999 SCT025= -->")

        with patch.dict(os.environ, {"KNMI_API_KEY": "key"}):
            result = get_daily_taf("EHAM", date(2026, 10, 12), opener=opener)

        self.assertEqual(result["raw"], "TAF EHAM 121100Z 1212/1318 24010KT 9999 SCT025")
        self.assertEqual(len(urls), 3)

    def test_open_data_api_searches_all_files_when_filenames_are_not_airport_specific(self):
        filenames = [f"report-{index}.xml" for index in range(5)]
        urls = []

        def opener(request, timeout):
            url = request.full_url
            urls.append(url)
            if "/datasets/taf/versions/1.0/files?" in url:
                return FakeResponse(json.dumps({"files": [{"filename": name} for name in filenames]}))
            if url.endswith("/url"):
                filename = url.split("/files/", 1)[1].removesuffix("/url")
                return FakeResponse(json.dumps({"temporaryDownloadUrl": f"https://download.example/{filename}"}))
            if url.endswith("report-4.xml"):
                return FakeResponse(f"<!-- {REPORT}= -->")
            return FakeResponse("<!-- TAF EHGG 121100Z 1212/1318 24010KT 9999 SCT025= -->")

        with patch.dict(os.environ, {"KNMI_API_KEY": "key"}):
            result = get_daily_taf("EHAM", date(2026, 10, 12), opener=opener)

        self.assertEqual(result["raw"], REPORT)
        self.assertEqual(len(urls), 11)

    def test_taf_fetch_shares_the_metar_rate_limit_table(self):
        with patch.dict(os.environ, {"KNMI_API_KEY": "key"}), \
                patch.object(taf_module, "_get_open_data_taf", return_value=REPORT):
            get_daily_taf("EHAM", date(2026, 10, 12))

        conn = taf_module._connect_db()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metar_api_call").fetchone()[0], 1)
        finally:
            conn.close()

    def test_refresh_fetches_each_airport_once_and_continues_after_failure(self):
        seen = []

        def fake_get(code, day, opener=None):
            seen.append(code)
            if code == "EHRD":
                raise TafError("down")

        with patch.object(taf_module, "get_daily_taf", fake_get):
            refresh_daily_tafs(date(2026, 10, 12))

        self.assertEqual(seen, ["EHAM", "EHRD", "EHGG", "EHLE"])

    def test_refresh_loop_waits_30_minutes_between_refreshes(self):
        class Stop:
            def __init__(self):
                self.waits = []

            def is_set(self):
                return bool(self.waits)

            def wait(self, seconds):
                self.waits.append(seconds)

        stop = Stop()
        with patch.object(taf_module, "refresh_daily_tafs") as refresh:
            taf_refresh_loop(stop)

        refresh.assert_called_once()
        self.assertEqual(stop.waits, [1800])


class TafQuestionTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_taf(REPORT)

    def test_questions_cover_header_base_and_change_groups(self):
        fields = {q["field"] for q in build_questions(self.parsed, "seed", "NL", DEFINITIONS)}

        for expected in (
            "taf_issued", "taf_valid", "taf_hours", "wind_direction", "wind_speed", "wind_gust",
            "visibility", "weather", "clouds", "taf_change_count", "taf_change_type", "taf_change_wind",
        ):
            self.assertIn(expected, fields)
        self.assertNotIn("wind_direction_variation", fields)
        self.assertNotIn("temperature", fields)
        self.assertNotIn("qnh", fields)

    def test_questions_have_unique_options_with_the_correct_one(self):
        for language in ("NL", "EN"):
            for q in build_questions(self.parsed, "seed", language, DEFINITIONS):
                self.assertEqual(len(q["options"]), len(set(q["options"])), q["field"])
                self.assertIn(q["correct"], q["options"])
                self.assertNotIn("METAR", q["question"])

    def test_issue_time_and_validity_answers(self):
        questions = {q["field"]: q for q in build_questions(self.parsed, "seed", "NL", DEFINITIONS)}

        self.assertEqual(questions["taf_issued"]["correct"], "11:00 UTC")
        self.assertEqual(questions["taf_valid"]["correct"], "dag 12, 12:00 UTC – dag 13, 18:00 UTC")
        self.assertEqual(questions["taf_hours"]["correct"], "30 uur")
        self.assertEqual(questions["taf_change_count"]["correct"], "4")

    def test_same_seed_gives_same_questions(self):
        first = build_questions(self.parsed, "seed", "NL", DEFINITIONS)
        second = build_questions(self.parsed, "seed", "NL", DEFINITIONS)

        self.assertEqual(first, second)

    def test_taf_without_changes_skips_change_detail_questions(self):
        parsed = parse_taf("TAF EHAM 121100Z 1212/1318 24010KT 9999 SCT025")
        fields = {q["field"] for q in build_questions(parsed, "seed", "NL", DEFINITIONS)}

        self.assertIn("taf_change_count", fields)
        self.assertNotIn("taf_change_type", fields)
        self.assertNotIn("taf_change_wind", fields)

    def test_grading_marks_selected_options(self):
        questions = build_questions(self.parsed, "seed", "NL", DEFINITIONS)
        submitted = {f"q_{q['field']}": q["correct"] for q in questions}
        submitted[f"q_{questions[0]['field']}"] = "wrong"
        results = grade_answers(questions, submitted)

        self.assertFalse(results[0]["correct"])
        self.assertTrue(all(item["correct"] for item in results[1:]))


class TafAbbreviationTests(unittest.TestCase):
    def test_taf_specific_abbreviations_are_in_the_database(self):
        from metar import load_abbreviations

        abbreviations = load_abbreviations(DEFAULT_LOCAL_DB)
        for code in ("TAF", "PROB30", "PROB40", "NSW"):
            self.assertIn(code, abbreviations)

    def test_questions_use_taf_examples_and_skip_metar_only_codes(self):
        seen = set()
        for seed in range(40):
            for q in build_abbreviation_questions(f"{seed:016x}", "NL", DEFINITIONS):
                seen.add(q["label"])
                before, token, after = q["example_parts"]
                self.assertTrue(token)
                self.assertNotIn("METAR", before + after)
                self.assertNotIn("Q1013", before + after)
                self.assertIn("TAF", q["question"])
                self.assertEqual(len(q["options"]), len(set(q["options"])))
                self.assertIn(q["correct"], q["options"])

        self.assertTrue({"TAF", "PROB30", "NSW", "TEMPO", "BECMG"} <= seen)
        self.assertFalse(seen & {"METAR", "SPECI", "RVR", "NOSIG", "QNH"})

    def test_english_questions(self):
        questions = build_abbreviation_questions("0" * 16, "EN", DEFINITIONS)

        self.assertEqual(len(questions), 10)
        self.assertTrue(all("mean in a TAF" in q["question"] for q in questions))


def make_taf_questions(parsed, seed, language="NL"):
    return build_questions(parsed, seed, language, DEFINITIONS)


def csrf_data(client, **data):
    with client.session_transaction() as session:
        token = session.get("csrf_token")
        if not token:
            token = "test-csrf-token"
            session["csrf_token"] = token
    return {"csrf_token": token, **data}


class TafRouteTests(unittest.TestCase):
    def test_switching_airport_loads_new_airport_and_clears_stale_selection(self):
        import app as app_module
        from app import app
        from metar import DUTCH_AIRPORTS

        parsed_ehle = parse_taf("TAF EHLE 121715Z 1218/1324 12008KT CAVOK")
        parsed_ehle.update({"airport": "EHLE", "airport_name": DUTCH_AIRPORTS["EHLE"], "date": "2026-10-12"})
        parsed_eham = parse_taf(REPORT)
        parsed_eham.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-10-12"})

        def fake_get_daily_taf(airport, *args, **kwargs):
            if airport == "EHLE":
                return parsed_ehle
            return parsed_eham

        client = app.test_client()
        with patch.object(app_module, "get_daily_taf", fake_get_daily_taf), patch.object(
            app_module, "load_weather_definitions", return_value=DEFINITIONS
        ):
            # 1. Load EHLE
            res_ehle = client.post("/taf/ontleden", data=csrf_data(client, airport="EHLE"))
            self.assertEqual(res_ehle.status_code, 200)
            self.assertIn("EHLE · Lelystad", res_ehle.get_data(as_text=True))

            # 2. Switch airport dropdown to EHAM while on EHLE page (which includes question_airport=EHLE)
            res_eham = client.post(
                "/taf/ontleden",
                data=csrf_data(client, airport="EHAM", question_airport="EHLE"),
            )
            self.assertEqual(res_eham.status_code, 200)
            html_eham = res_eham.get_data(as_text=True)
            self.assertIn("EHAM · Amsterdam Schiphol", html_eham)
            self.assertNotIn("EHLE · Lelystad", html_eham)
