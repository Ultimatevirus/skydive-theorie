import os
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import date
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import patch

import app as app_module
import metar as metar_module
from app import app
from db import DEFAULT_LOCAL_DB
from metar import (
    DUTCH_AIRPORTS,
    MetarError,
    _CACHE,
    build_abbreviation_questions,
    build_questions,
    get_daily_metar,
    grade_answers,
    load_weather_definitions,
    metar_refresh_loop,
    parse_metar,
    refresh_daily_metars,
)


_access_gate_patcher = patch.dict(os.environ, {"ACCESS_GATE_ENABLED": "0"})


def setUpModule():
    _access_gate_patcher.start()


def tearDownModule():
    _access_gate_patcher.stop()


REPORT = "EHAM 021255Z 27012G20KT 9999 -RA SCT020 BKN035 12/08 Q1013"
DEFINITIONS = load_weather_definitions(DEFAULT_LOCAL_DB)


def make_questions(parsed, seed, language="NL"):
    return build_questions(parsed, seed, language, DEFINITIONS)


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

    def test_forced_refresh_replaces_same_day_database_and_memory_cache(self):
        day = date(2026, 9, 2)
        old_report = REPORT.replace("27012G20KT", "25008KT")
        metar_module._store_metar(day, "EHAM", old_report)
        _CACHE[(day.isoformat(), "EHAM")] = metar_module._build_result(old_report, "EHAM", day)

        with patch.dict("os.environ", {"KNMI_API_KEY": "test-key", "KNMI_METAR_URL": ""}), \
                patch("metar._wait_for_api_slot", return_value=True), \
                patch("metar._get_open_data_metar", return_value=REPORT):
            result = get_daily_metar("EHAM", day, force_refresh=True)

        self.assertEqual(result["raw"], REPORT)
        self.assertEqual(_CACHE[(day.isoformat(), "EHAM")]["raw"], REPORT)
        self.assertEqual(metar_module._get_cached_metar(day, "EHAM"), REPORT)

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
        self.assertEqual(
            opener.call_args_list[0].args[0].get_header("User-agent"),
            metar_module.USER_AGENT,
        )
        conn = sqlite3.connect(self.database.name)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metar_api_call").fetchone()[0], 3)
        finally:
            conn.close()

    def test_api_retries_rate_limit_responses_with_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "3"
        response = unittest.mock.MagicMock()
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        opener = unittest.mock.Mock(side_effect=[
            HTTPError("https://example.test", 429, "Too Many Requests", headers, None),
            response,
        ])

        with patch("metar._wait_for_api_slot", return_value=True), \
                patch.object(metar_module, "_LAST_API_REQUEST", None), \
                patch.object(metar_module, "_API_REQUEST_INTERVAL", 0), \
                patch("metar.time.sleep") as sleep:
            result = metar_module._request(
                "https://example.test", "test-key", opener, "application/json"
            )

        self.assertEqual(result, b"{}")
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(3.0)
        self.assertEqual(
            opener.call_args_list[1].args[0].get_header("User-agent"),
            metar_module.USER_AGENT,
        )

    def test_api_request_quota_counts_actual_requests_in_the_hourly_window(self):
        with patch.object(metar_module, "API_CALL_LIMIT", 2):
            self.assertTrue(metar_module._wait_for_api_slot(wait=False))
            self.assertTrue(metar_module._wait_for_api_slot(wait=False))
            self.assertFalse(metar_module._wait_for_api_slot(wait=False))

        conn = sqlite3.connect(self.database.name)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM metar_api_call").fetchone()[0], 2)
        finally:
            conn.close()

    def test_api_requests_are_spaced_at_least_one_second_apart(self):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        opener = unittest.mock.Mock()
        response = unittest.mock.MagicMock()
        response.read.return_value = b"{}"
        response.__enter__.return_value = response
        opener.return_value = response

        with patch.object(metar_module, "_LAST_API_REQUEST", None), \
                patch.object(metar_module.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(metar_module.time, "sleep", side_effect=sleep) as sleep_mock:
            metar_module._request("https://example.test/one", "", opener, "application/json")
            metar_module._request("https://example.test/two", "", opener, "application/json")

        sleep_mock.assert_called_once_with(1.0)
        self.assertEqual(opener.call_count, 2)

    def test_refresh_fetches_each_airport_once_and_continues_after_failure(self):
        calls = []

        def fetch(code, day, opener, force_refresh):
            calls.append((code, force_refresh))
            if code == "EHRD":
                raise MetarError("temporary")
            return {}

        with patch("metar.get_daily_metar", side_effect=fetch):
            refresh_daily_metars(date(2026, 9, 3))

        self.assertEqual(calls, [(code, True) for code in dict.fromkeys(DUTCH_AIRPORTS)])

    def test_notification_only_refreshes_airport_named_in_event(self):
        with patch("metar.get_daily_metar") as get_metar:
            refresh_daily_metars(date(2026, 9, 3), airports=["EHAM"])

        get_metar.assert_called_once_with(
            "EHAM", date(2026, 9, 3), opener=unittest.mock.ANY, force_refresh=True
        )

    def test_notification_extracts_airport_from_knmi_filename(self):
        self.assertEqual(
            metar_module.METAR_NOTIFICATION_TOPIC,
            "dataplatform/file/v1/metar/1.0/created",
        )
        self.assertEqual(
            metar_module._notification_airports(
                b'{"filename":"A_LANL80EHAM022022_C_EHAM_020926202202.xml"}'
            ),
            ["EHAM"],
        )
        self.assertIsNone(metar_module._notification_airports(b'{"dataset":"metar"}'))

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

    def test_questions_have_four_unique_options_with_the_correct_one(self):
        for report in (
            REPORT,
            "EHRD 022125Z AUTO VRB03KT CAVOK 19/14 Q1018",
            "EHRD 022125Z 23013KT 200V260 3000 +SHRA BKN033 M02/M04 Q0998",
        ):
            parsed = parse_metar(report)
            for seed in range(30):
                for question in make_questions(parsed, str(seed)):
                    self.assertEqual(len(question["options"]), 4, question)
                    self.assertEqual(len(set(question["options"])), 4, question)
                    self.assertIn(question["correct"], question["options"])

    def test_questions_cover_issue_time_and_wind_with_close_distractors(self):
        questions = {q["field"]: q for q in make_questions(parse_metar(REPORT), "abc")}

        self.assertEqual(questions["issued_time"]["correct"], "12:55 UTC")
        self.assertEqual(questions["wind_speed"]["correct"], "12 kt")
        speeds = {int(option.split()[0]) for option in questions["wind_speed"]["options"]}
        self.assertTrue(all(abs(speed - 12) <= 5 for speed in speeds))
        self.assertEqual(make_questions(parse_metar(REPORT), "abc"), list(questions.values()))

    def test_grading_marks_selected_options(self):
        questions = make_questions(parse_metar(REPORT), "seed")
        answers = {f"q_{q['field']}": q["correct"] for q in questions}
        self.assertTrue(all(item["correct"] for item in grade_answers(questions, answers)))

        answers["q_wind_speed"] = next(o for o in questions[2]["options"] if o != questions[2]["correct"])
        result = grade_answers(questions, answers)
        self.assertFalse(next(item for item in result if item["field"] == "wind_speed")["correct"])
        self.assertEqual(sum(not item["correct"] for item in result), 1)

    def test_weather_question_asks_for_the_meaning(self):
        parsed = parse_metar("EHRD 022125Z 23013KT 3000 -SHRA BR BKN033 19/14 Q1018")
        nl = next(q for q in make_questions(parsed, "x") if q["field"] == "weather")
        en = next(q for q in make_questions(parsed, "x", "EN") if q["field"] == "weather")

        self.assertIn("Wat betekent", nl["question"])
        self.assertEqual(nl["correct"], "Lichte regenbui; Nevel (zicht 1000-5000 m)")
        self.assertEqual(en["correct"], "Light rain shower; Mist (visibility 1000-5000 m)")
        self.assertNotIn("-SHRA", " ".join(nl["options"]))

    def test_weather_definitions_come_from_the_database(self):
        definitions = load_weather_definitions(DEFAULT_LOCAL_DB)

        self.assertEqual(definitions["FZFG"], ("Onderkoelde mist", "Freezing fog"))

    def test_weather_definitions_are_cached_and_refresh_when_database_changes(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE metar_weather_codes (
                METAR_abreviation TEXT,
                NL_definition TEXT,
                EN_definition TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO metar_weather_codes VALUES (?, ?, ?)",
            ("RA", "Regen", "Rain"),
        )
        conn.commit()
        conn.close()

        try:
            with patch.object(
                metar_module,
                "connect_db",
                wraps=metar_module.connect_db,
            ) as connect:
                first = load_weather_definitions(db_path)
                second = load_weather_definitions(db_path)

                self.assertEqual(first, {"RA": ("Regen", "Rain")})
                self.assertEqual(second, first)
                connect.assert_called_once()

                conn = sqlite3.connect(db_path)
                conn.execute(
                    "INSERT INTO metar_weather_codes VALUES (?, ?, ?)",
                    ("SN", "Sneeuw", "Snow"),
                )
                conn.commit()
                conn.close()

                updated = load_weather_definitions(db_path)
                self.assertEqual(updated["SN"], ("Sneeuw", "Snow"))
                self.assertEqual(connect.call_count, 2)
        finally:
            os.remove(db_path)

    def test_parser_reads_issue_time(self):
        self.assertEqual(parse_metar(REPORT)["issued_time"], "12:55")


class MetarRouteTests(unittest.TestCase):
    def test_get_and_post_metar_page(self):
        parsed = parse_metar(REPORT)
        parsed.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", return_value=parsed), patch.object(
            app_module, "load_weather_definitions", return_value=DEFINITIONS
        ):
            response = client.get("/metar/ontleden?airport=EHAM")
            self.assertEqual(response.status_code, 200)
            self.assertIn(REPORT, response.get_data(as_text=True))
            self.assertIn("Hoe laat is deze METAR uitgegeven?", response.get_data(as_text=True))
            with client.session_transaction() as session:
                seed = session["metar_seed"]

            answers = {f"q_{q['field']}": q["correct"] for q in make_questions(parsed, seed)}
            response = client.post(
                "/metar/ontleden",
                data=csrf_data(client, airport="EHAM", action="check", **answers),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(f"{len(answers)} / {len(answers)}", response.get_data(as_text=True))
        self.assertIn("result-correct", response.get_data(as_text=True))
        self.assertNotIn("result-wrong", response.get_data(as_text=True))

    def test_mismatched_attempt_metadata_does_not_override_the_generated_airport(self):
        parsed = parse_metar(REPORT)
        parsed.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", return_value=parsed), patch.object(
            app_module, "load_weather_definitions", return_value=DEFINITIONS
        ):
            client.get("/metar/ontleden?airport=EHAM")
            with client.session_transaction() as session:
                attempt_id = session["metar_attempt_id"]
                seed = session["metar_seed"]
            answers = {f"q_{q['field']}": q["correct"] for q in make_questions(parsed, seed)}
            response = client.post(
                "/metar/ontleden",
                data=csrf_data(
                    client,
                    airport="EHRD",
                    question_airport="EHRD",
                    attempt_id=attempt_id,
                    action="check",
                    **answers,
                ),
            )

        html = response.get_data(as_text=True)
        self.assertIn("EHAM · Amsterdam Schiphol", html)
        self.assertNotIn("EHRD · Rotterdam The Hague", html)

    def test_grading_uses_the_metar_shown_when_the_attempt_started(self):
        original = parse_metar(REPORT)
        original.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        refreshed = parse_metar("EHAM 021300Z 18005KT 8000 RA BKN010 10/09 Q1000")
        refreshed.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", side_effect=[original, refreshed]) as get_metar, patch.object(
            app_module, "load_weather_definitions", return_value=DEFINITIONS
        ):
            client.get("/metar/ontleden?airport=EHAM")
            with client.session_transaction() as session:
                attempt_id = session["metar_attempt_id"]
                seed = session["metar_seed"]
            answers = {f"q_{q['field']}": q["correct"] for q in make_questions(original, seed)}
            response = client.post(
                "/metar/ontleden",
                data=csrf_data(client, airport="EHAM", attempt_id=attempt_id, action="check", **answers),
            )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(REPORT, html)
        self.assertNotIn(refreshed["raw"], html)
        self.assertIn(f"{len(answers)} / {len(answers)}", html)
        get_metar.assert_called_once()

    def test_practice_attempt_snapshots_are_bounded_in_the_session(self):
        parsed = parse_metar(REPORT)
        parsed.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", return_value=parsed) as get_metar, patch.object(
            app_module, "load_weather_definitions", return_value=DEFINITIONS
        ):
            client.get("/metar/ontleden?airport=EHAM")
            with client.session_transaction() as session:
                expired_attempt_id = session["metar_attempt_id"]
                csrf_token = session["csrf_token"]

            for index in range(12):
                response = client.post(
                    "/metar/ontleden",
                    data={
                        "csrf_token": csrf_token,
                        "action": "load",
                        "airport": "EHAM",
                        "attempt_id": f"{index:032x}",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(len(response.headers["Set-Cookie"]), app.config["MAX_COOKIE_SIZE"])

            with client.session_transaction() as session:
                self.assertEqual(len(session["metar_attempts"]), app_module.MAX_PRACTICE_ATTEMPTS)

            expired = client.post(
                "/metar/ontleden",
                data=csrf_data(
                    client,
                    airport="EHAM",
                    attempt_id=expired_attempt_id,
                    action="check",
                ),
            )

        self.assertIn("Deze oefening is verlopen", expired.get_data(as_text=True))
        self.assertEqual(get_metar.call_count, 13)

    def test_unanswered_questions_are_marked_and_counted_wrong(self):
        parsed = parse_metar(REPORT)
        parsed.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", return_value=parsed), patch.object(
            app_module, "load_weather_definitions", return_value=DEFINITIONS
        ):
            client.get("/metar/ontleden?airport=EHAM")
            response = client.post("/metar/ontleden", data=csrf_data(client, airport="EHAM", action="check"))

        html = response.get_data(as_text=True)
        self.assertIn("result-unanswered", html)
        self.assertIn("answer-unanswered", html)
        self.assertIn("0 / ", html)

    def test_loading_another_airport_uses_that_selection(self):
        eham = parse_metar(REPORT)
        eham.update({"airport": "EHAM", "airport_name": DUTCH_AIRPORTS["EHAM"], "date": "2026-09-02"})
        ehrd = parse_metar(REPORT.replace("EHAM", "EHRD"))
        ehrd.update({"airport": "EHRD", "airport_name": DUTCH_AIRPORTS["EHRD"], "date": "2026-09-02"})
        client = app.test_client()

        with patch.object(app_module, "get_daily_metar", side_effect=[eham, ehrd]) as get_metar:
            client.post("/metar/ontleden", data=csrf_data(client, airport="EHAM", action="load"))
            response = client.post("/metar/ontleden", data=csrf_data(client, airport="EHRD", action="load"))

        self.assertEqual(response.status_code, 200)
        self.assertIn("EHRD · Rotterdam The Hague", response.get_data(as_text=True))
        self.assertEqual(get_metar.call_args_list[1].args[0], "EHRD")

    def test_invalid_airport_is_rejected(self):
        response = app.test_client().get("/metar/ontleden?airport=XXXX")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Kies een Nederlandse luchthaven", response.get_data(as_text=True))


class MetarAbbreviationTests(unittest.TestCase):
    def test_questions_are_deterministic_with_plausible_unique_options(self):
        first = build_abbreviation_questions("a" * 16, "NL", DEFINITIONS)
        second = build_abbreviation_questions("a" * 16, "NL", DEFINITIONS)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 10)
        self.assertEqual(len({q["field"] for q in first}), 10)
        for question in first:
            self.assertEqual(len(question["options"]), 4)
            self.assertEqual(len(set(question["options"])), 4)
            self.assertIn(question["correct"], question["options"])

    def test_cloud_distractors_are_other_cloud_meanings(self):
        cloud_meanings = {
            nl for cat, nl, _, _ in metar_module.load_abbreviations(DEFAULT_LOCAL_DB).values() if cat == "cloud"
        }
        for seed in range(30):
            for question in build_abbreviation_questions(f"{seed:016x}", "NL", DEFINITIONS, count=200):
                if question["label"] == "BKN":
                    self.assertTrue(set(question["options"]) <= cloud_meanings)
                    return
        self.fail("BKN question never generated")

    def test_english_language_uses_english_meanings(self):
        questions = build_abbreviation_questions("b" * 16, "EN", DEFINITIONS, count=200)
        by_code = {q["label"]: q for q in questions}

        self.assertEqual(by_code["BKN"]["correct"], "Broken clouds (5-7 oktas)")
        self.assertIn("mean in a METAR", by_code["BKN"]["question"])

    def test_every_question_has_an_example_metar_containing_the_code(self):
        questions = build_abbreviation_questions("c" * 16, "NL", DEFINITIONS, count=500)
        self.assertGreater(len(questions), 80)
        for question in questions:
            before, token, after = question["example_parts"]
            self.assertTrue(token, question["label"])
            self.assertTrue(before or after, question["label"])

    def test_abbreviation_routes(self):
        client = app.test_client()
        choice = client.get("/metar").get_data(as_text=True)
        self.assertIn("/oefenen/meteo/METAR-TAF/keuze/ontleden", choice)
        self.assertIn("/oefenen/meteo/METAR-TAF/keuze/afkortingen", choice)

        page = client.get("/metar/afkortingen")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(html.count('<input type="radio"'), 40)

        seed = html.split('name="seed" value="')[1].split('"')[0]
        questions = build_abbreviation_questions(seed, "NL", DEFINITIONS)
        answers = {f"q_{q['field']}": q["correct"] for q in questions}
        graded = client.post(
            "/metar/afkortingen",
            data=csrf_data(client, action="check", seed=seed, **answers),
        ).get_data(as_text=True)
        self.assertIn("10 / 10", graded)

        empty = client.post(
            "/metar/afkortingen", data=csrf_data(client, action="check", seed=seed)
        ).get_data(as_text=True)
        self.assertIn("0 / 10", empty)

        invalid = client.post(
            "/metar/afkortingen", data=csrf_data(client, action="check", seed="bad")
        ).get_data(as_text=True)
        self.assertEqual(invalid.count('<input type="radio"'), 40)


if __name__ == "__main__":
    unittest.main()
