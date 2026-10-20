import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as app_module
import taf as taf_module
from app import app


_access_gate_patcher = patch.dict(os.environ, {"ACCESS_GATE_ENABLED": "0"})


def setUpModule():
    _access_gate_patcher.start()


def tearDownModule():
    _access_gate_patcher.stop()


def csrf_data(client, **data):
    with client.session_transaction() as session:
        token = session.get("csrf_token")
        if not token:
            token = "test-csrf-token"
            session["csrf_token"] = token
    return {"csrf_token": token, **data}


class PracticeFlowTests(unittest.TestCase):
    def test_recent_template_strings_are_in_english_translation_map(self):
        required = [
            "Oefenen voor je KNVvL A/B brevet",
            "Op deze pagina vind je dynamische oefeningen om te oefenen voor de examens.",
            "De dynamische oefeningen voor dit onderwerp worden hier verzameld.",
            "Let op!: Dit zijn geen officiële KNVvL (oefen)examenvragen!",
            "Op dit moment zijn de vragen nog NIET gevalideerd!",
            "METAR mode",
            "Na het kiezen van een luchthaven krijg je de actuele METAR te zien, waarna er een aantal vragen volgen. Deze vragen gaan over de inhoud van de METAR en worden dynamisch gegenereerd. De METAR's worden bijgewerkt zodra KNMI een nieuw METAR-bestand beschikbaar maakt.",
            "Met een oefenexamen maak je een vaste set van 40 willekeurige vragen. Bij vrij oefenen kies je zelf het aantal vragen en de onderwerpen.",
            "Kies een luchthaven en decodeer de actuele METAR. Let goed op de voorbeeldinvoer voordat je begint. De METAR's worden bijgewerkt zodra KNMI een nieuw METAR-bestand beschikbaar maakt.",
            "Selecteer een luchthaven om de dagelijkse METAR te laden.",
            "Deze website is ontworpen om mensen te ondersteunen bij het leren voor de KNVvL theorie-examens, voornamelijk voor het A of B brevet.",
            "De vragen op deze site zijn ontworpen om beginnende skydivers te helpen met de voorbereiding op de theorie-examens voor het A of B brevet. Ze zijn niet officieel en dienen alleen ter oefening. Als je vragen hebt over de inhoud van de examens, is het aan te raden om contact op te nemen met een (door de KNVvL) erkend paracentrum. Zij kunnen je voorzien van de meest actuele en accurate informatie over de examens en de vereisten voor het behalen van je brevet. Voor algemene leertips en adviezen kan je de pagina \"leren voor het examen\" bezoeken",
            "Voor de KNVvL theorie-examens is het belangrijk om niet alleen vragen te oefenen, maar ook de achterliggende stof goed door te nemen. Manieren om dit aan te pakken zijn bijvoorbeeld het doornemen van het BVR en de bijlagen, uitgebreidere weerberichten lezen, praten met instructeurs, hulpinstructeurs en riggers en documentatie van fabricanten lezen. Deze website kan je daarnaast gebruiken als een overzicht van mogelijke leerpunten, een voorproef op het examen of als startpunt voor verdere verdieping.",
            "Ontwikkelings roadmap",
        ]

        for key in required:
            self.assertIn(key, app_module.TRANSLATIONS["EN"])

    def test_practice_mode_explanation_is_displayed_in_english(self):
        client = app.test_client()
        client.post("/language", data=csrf_data(client, language="EN"))

        response = client.get("/practice/mode/A")

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "A practice exam gives you a fixed set of 40 random questions. In free practice, you choose the number of questions and the topics.",
            response.get_data(as_text=True),
        )

    def test_oefenen_pages_link_to_categories_and_metar_taf_modes(self):
        client = app.test_client()
        client.post("/language", data=csrf_data(client, language="EN"))

        home = client.get("/oefenen")
        html = home.get_data(as_text=True)
        self.assertEqual(home.status_code, 200)
        self.assertIn(
            "This page contains dynamic exercises to help you practice for the exams.",
            html,
        )
        self.assertNotIn('href="/metar"', html)
        for category in ("algemeen", "regelementen", "meteo", "material"):
            self.assertIn(f'href="/oefenen/{category}"', html)

        meteo = client.get("/oefenen/meteo")
        self.assertEqual(meteo.status_code, 200)
        self.assertIn('href="/oefenen/meteo/METAR-TAF"', meteo.get_data(as_text=True))

        metar_taf = client.get("/oefenen/meteo/METAR-TAF")
        exercise_html = metar_taf.get_data(as_text=True)
        self.assertEqual(metar_taf.status_code, 200)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/keuze/ontleden"', exercise_html)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/keuze/afkortingen"', exercise_html)

        self.assertEqual(client.get("/oefenen/unknown").status_code, 404)

    def test_metar_practice_shows_report_in_question_and_original_panel(self):
        metar = {"airport": "EHAM", "airport_name": "Amsterdam", "raw": "EHAM 101425Z 24012KT"}
        question = {
            "question": "What is the wind direction?",
            "options": ["240 degrees", "120 degrees"],
            "field": "wind_direction",
        }

        with (
            patch.object(app_module, "get_daily_metar", return_value=metar),
            patch.object(app_module, "load_weather_definitions", return_value={}),
            patch.object(app_module, "build_questions", return_value=[question]),
        ):
            response = app.test_client().get("/metar/ontleden")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="metar-question-heading"', html)
        self.assertIn('metar-question-cell metar-question-report', html)
        self.assertGreaterEqual(html.count(metar["raw"]), 2)

    def test_taf_practice_shows_multiline_report(self):
        taf = taf_module.parse_taf(
            "TAF EHAM 121100Z 1212/1318 24010KT 9999 SCT025 BECMG 1214/1216 27015KT"
        )
        taf.update(airport="EHAM", airport_name="Amsterdam")

        with (
            patch.object(app_module, "get_daily_taf", return_value=taf),
            patch.object(app_module, "load_weather_definitions", return_value={}),
            patch.object(app_module.taf_module, "build_questions", return_value=[
                {"field": "taf_issued", "label": "x", "question": "Q?", "options": ["a", "b"], "correct": "a"},
            ]),
        ):
            response = app.test_client().get("/taf/ontleden")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('class="taf-lines"', html)
        self.assertIn("<code>BECMG 1214/1216 27015KT</code>", html)
        self.assertIn("Controleer TAF", html)

    def test_taf_practice_grades_answers(self):
        taf = taf_module.parse_taf("TAF EHAM 121100Z 1212/1318 24010KT 9999 SCT025")
        taf.update(airport="EHAM", airport_name="Amsterdam")
        client = app.test_client()

        with (
            patch.object(app_module, "get_daily_taf", return_value=taf),
            patch.object(app_module, "load_weather_definitions", return_value={}),
            patch.object(app_module.taf_module, "build_questions", return_value=[
                {"field": "taf_issued", "label": "x", "question": "Q?", "options": ["a", "b"], "correct": "a"},
            ]),
        ):
            client.get("/taf/ontleden")
            response = client.post(
                "/taf/ontleden",
                data=csrf_data(client, action="check", airport="EHAM", q_taf_issued="a"),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Je score: 1 / 1", response.get_data(as_text=True))

    def test_taf_practice_reports_service_errors_and_invalid_airport(self):
        client = app.test_client()
        with patch.object(app_module, "get_daily_taf", side_effect=taf_module.TafError("De TAF wordt nog opgehaald.")):
            self.assertIn("De TAF wordt nog opgehaald.", client.get("/taf/ontleden").get_data(as_text=True))
        invalid = client.get("/taf/ontleden?airport=EGLL")
        self.assertIn("Kies een Nederlandse luchthaven", invalid.get_data(as_text=True))

    def test_taf_abbreviations_page_and_choice_page(self):
        client = app.test_client()
        page = client.get("/taf/afkortingen")
        choice = client.get("/metar").get_data(as_text=True)

        self.assertEqual(page.status_code, 200)
        self.assertIn("<mark>", page.get_data(as_text=True))
        self.assertIn("METAR/TAF oefenen", choice)
        self.assertIn("Wil je ontleden oefenen of afkortingen?", choice)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/keuze/ontleden"', choice)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/keuze/afkortingen"', choice)
        self.assertNotIn('href="/oefenen/meteo/METAR-TAF/ontleden"', choice)

    def test_metar_type_choice_shows_only_report_types_for_selected_mode(self):
        client = app.test_client()

        parsing = client.get("/metar/keuze/ontleden").get_data(as_text=True)
        abbreviations = client.get("/metar/keuze/afkortingen").get_data(as_text=True)

        self.assertIn("Wil je METAR&#39;s of TAF&#39;s oefenen?", parsing)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/ontleden"', parsing)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/taf/ontleden"', parsing)
        self.assertNotIn('href="/oefenen/meteo/METAR-TAF/afkortingen"', parsing)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/afkortingen"', abbreviations)
        self.assertIn('href="/oefenen/meteo/METAR-TAF/taf/afkortingen"', abbreviations)
        self.assertNotIn('href="/oefenen/meteo/METAR-TAF/ontleden"', abbreviations)
        self.assertEqual(client.get("/metar/keuze/invalid").status_code, 404)

    def test_page_load_does_not_hide_content_before_assets_finish_loading(self):
        transition_script = (Path(app_module.BASE_DIR) / "static" / "page-transition.js").read_text()
        stylesheet = (Path(app_module.BASE_DIR) / "static" / "style.css").read_text()
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        background_response = client.get("/static/branding/background.webp")

        self.assertNotIn("page-entering", transition_script)
        self.assertIn("page-leaving", transition_script)
        self.assertNotIn("page-entering", stylesheet)
        self.assertIn("background-color: #eef6fb", stylesheet)
        self.assertIn('rel="preload" as="image"', html)
        self.assertIn('fetchpriority="high"', html)
        self.assertIn("branding/background.webp", html)
        self.assertIn("branding/logo.webp", html)
        self.assertIn('href="/"', html)
        self.assertIn('class="brand-link"', html)
        self.assertEqual(background_response.status_code, 200)
        self.assertIn("public", background_response.headers["Cache-Control"])
        self.assertIn("max-age=31536000", background_response.headers["Cache-Control"])

    def test_health_check_is_available_for_reverse_proxy(self):
        response = app.test_client().get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_it_systems_status_page_is_localized_and_uses_persisted_values(self):
        client = app.test_client()
        states = {
            "smtp": {"status": "operational", "checked_at": 1_797_000_000, "last_activity_at": None},
            "knmi_mqtt": {
                "status": "operational",
                "checked_at": 1_797_000_000,
                "last_activity_at": 1_796_999_900,
            },
            "knmi_opendata": {
                "status": "operational",
                "checked_at": 1_797_000_000,
                "last_activity_at": None,
            },
        }

        client.post("/language", data=csrf_data(client, language="EN"))
        with patch.object(app_module, "get_service_statuses", return_value=states):
            response = client.get("/it-systemen")

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("All systems operational", html)
        self.assertIn("Last checked:", html)
        self.assertIn("Last KNMI notification:", html)

    def test_favicon_keeps_compatibility_urls_and_is_small(self):
        client = app.test_client()
        svg_response = client.get("/favicon.svg")
        ico_response = client.get("/favicon.ico")
        home_html = client.get("/").get_data(as_text=True)
        static_response = client.get(
            "/static/icons/book-solid-full.svg"
        )

        self.assertEqual(svg_response.status_code, 200)
        self.assertEqual(svg_response.mimetype, "image/svg+xml")
        self.assertLess(len(svg_response.data), 10_000)
        self.assertIn(b"<svg ", svg_response.data)
        self.assertEqual(ico_response.status_code, 200)
        self.assertIn(
            'href="/static/icons/book-solid-full.svg?v=',
            home_html,
        )
        self.assertEqual(static_response.status_code, 200)
        self.assertEqual(static_response.mimetype, "image/svg+xml")
        self.assertLess(len(static_response.data), 10_000)
        self.assertIn("max-age=31536000", static_response.headers["Cache-Control"])

    def test_security_headers_are_added_to_html_responses(self):
        response = app.test_client().get("/")

        self.assertEqual(response.headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(response.headers["Referrer-Policy"], "strict-origin-when-cross-origin")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_production_compose_keeps_knmi_metar_configuration(self):
        compose_path = Path(app_module.BASE_DIR) / "compose.production.yml"
        compose_text = compose_path.read_text(encoding="utf-8")

        self.assertIn("KNMI_API_KEY:", compose_text)
        self.assertIn("KNMI_OPEN_DATA_URL:", compose_text)
        self.assertIn("build:", compose_text)
        self.assertNotIn('"${APP_PORT:-5000}:5000"', compose_text)
        self.assertNotIn("APP_IMAGE must be set", compose_text)

    def test_language_selection_persists_and_localizes_pages(self):
        client = app.test_client()

        response = client.post("/language", data=csrf_data(client, language="EN"))

        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session["language"], "EN")

        response = client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn("Practice exam", html)
        self.assertIn('data-language="EN"', html)
        self.assertIn('language-option is-selected', html)
        self.assertIn('<html lang="en">', html)

    def test_gdpr_notice_is_bilingual_and_linked_from_footer(self):
        client = app.test_client()

        dutch_response = client.get("/gdpr")
        dutch_html = dutch_response.get_data(as_text=True)

        self.assertEqual(dutch_response.status_code, 200)
        self.assertIn("Server- en websitebezoeklogs", dutch_html)
        self.assertIn('href="/gdpr"', dutch_html)
        self.assertIn('href="/it-systemen"', dutch_html)
        self.assertIn(">EU GDPR</a>", dutch_html)

        client.post("/language", data=csrf_data(client, language="EN"))
        english_response = client.get("/gdpr")
        english_html = english_response.get_data(as_text=True)

        self.assertEqual(english_response.status_code, 200)
        self.assertIn("Server and website access logs", english_html)
        self.assertIn("Server access logs are retained for a maximum of <strong>30 days</strong>", english_html)

    def test_questions_are_filtered_by_language(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                level TEXT,
                language TEXT,
                topic TEXT,
                subtopic TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO questions (level, language, topic, subtopic, is_active, question, true_answer, false_answers) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("A", "NL", "Materiaal", "Harnas", 1, "Nederlands", "Ja", "[]"),
                ("A", "EN", "Equipment", "Harness", 1, "English", "Yes", "[]"),
            ],
        )
        conn.commit()
        conn.close()

        try:
            with patch.object(app_module, "get_db_path", return_value=db_path):
                questions = app_module.fetch_questions("A", 1, language="EN")

            self.assertEqual(len(questions), 1)
            self.assertEqual(questions[0]["question"], "English")
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_language_change_clears_old_free_practice_topic_filter(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                level TEXT,
                language TEXT,
                topic TEXT,
                subtopic TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO questions (level, language, topic, subtopic, is_active, question, true_answer, false_answers) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("A", "NL", "Materiaal", "Harnas", 1, "Nederlandse vraag", "Ja", "['Nee']"),
                ("A", "EN", "Equipment", "Harness", 1, "English question", "Yes", "['No']"),
            ],
        )
        conn.commit()
        conn.close()

        client = app.test_client()
        with client.session_transaction() as session:
            session.update(
                {
                    "csrf_token": "test-csrf-token",
                    "level": "A",
                    "question_amount": 1,
                    "selected_topics": ["Materiaal"],
                    "language": "NL",
                    "exam_question_ids": [1],
                    "started_at": 1000,
                }
            )

        try:
            app_module._language_column_cache.clear()
            with patch.object(app_module, "get_db_path", return_value=db_path):
                language_response = client.post(
                    "/language",
                    data=csrf_data(client, language="EN"),
                    headers={"Referer": "http://localhost/exam"},
                )
                self.assertEqual(language_response.status_code, 302)

                exam_response = client.get("/exam")
                self.assertEqual(exam_response.status_code, 200)
                self.assertIn("English question", exam_response.get_data(as_text=True))

                with client.session_transaction() as session:
                    question_ids = session["exam_question_ids"]
                    self.assertNotIn("selected_topics", session)

                result_response = client.post(
                    "/exam",
                    data=csrf_data(
                        client,
                        **{f"answer_{question_ids[0]}": "Yes"},
                    ),
                )

            self.assertEqual(result_response.status_code, 200)
            self.assertIn("Your score: 1 / 1", result_response.get_data(as_text=True))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_practice_page_shows_level_question_and_cards(self):
        client = app.test_client()
        response = client.get("/practice")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Welk brevet wil je voor oefenen?", html)
        self.assertIn("A", html)
        self.assertIn("B", html)

    def test_exam_submission_does_not_rebuild_options_for_result_page(self):
        client = app.test_client()
        questions = app_module.fetch_questions("A", 3)
        question_ids = [int(row["id"]) for row in questions]
        with client.session_transaction() as session:
            session.update(
                {
                    "csrf_token": "test-csrf-token",
                    "level": "A",
                    "question_amount": len(question_ids),
                    "selected_topics": None,
                    "language": "NL",
                    "exam_question_ids": question_ids,
                    "started_at": 1000,
                }
            )
        answers = {
            f"answer_{row['id']}": str(row["true_answer"])
            for row in questions
        }

        with patch.object(
            app_module,
            "build_option_list",
            side_effect=AssertionError("Result page does not need answer options"),
        ):
            response = client.post(
                "/exam",
                data={"csrf_token": "test-csrf-token", **answers},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Je score:", response.get_data(as_text=True))

    def test_exam_results_show_source_only_for_incorrect_answers_with_source(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE questions (
                level TEXT,
                topic TEXT,
                subtopic TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT,
                source TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO questions
                (level, topic, subtopic, is_active, question, true_answer, false_answers, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("A", "Materiaal", "Harnas", 1, "Wrong answer with source?", "Ja", "[]", "BVR20260303"),
                ("A", "Materiaal", "Harnas", 1, "Correct answer with source?", "Ja", "[]", "Should not be shown"),
                ("A", "Materiaal", "Harnas", 1, "Wrong answer without source?", "Ja", "[]", None),
            ],
        )
        conn.commit()
        conn.close()

        client = app.test_client()
        with client.session_transaction() as session:
            session.update(
                {
                    "csrf_token": "test-csrf-token",
                    "level": "A",
                    "question_amount": 3,
                    "selected_topics": None,
                    "language": "NL",
                    "exam_question_ids": [1, 2, 3],
                    "started_at": 1000,
                }
            )

        try:
            app_module._language_column_cache.clear()
            with patch.object(app_module, "get_db_path", return_value=db_path):
                response = client.post(
                    "/exam",
                    data={
                        "csrf_token": "test-csrf-token",
                        "answer_1": "Nee",
                        "answer_2": "Ja",
                        "answer_3": "Nee",
                    },
                )

            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Bron: BVR20260303", html)
            self.assertNotIn("Should not be shown", html)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_practice_images_show_localized_original_source(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE image_source (
                image_key TEXT PRIMARY KEY,
                image_full_name TEXT NOT NULL,
                image_local_source TEXT,
                image_original_source TEXT,
                start_date TEXT,
                end_date TEXT
            );
            INSERT INTO image_source VALUES (
                'example-image',
                'Example image',
                '/question_images/example.jpg',
                'https://source.example/image',
                NULL,
                NULL
            );
            CREATE TABLE questions (
                Key INTEGER PRIMARY KEY,
                level TEXT,
                topic TEXT,
                subtopic TEXT,
                language TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT,
                image_key TEXT REFERENCES image_source(image_key)
            );
            INSERT INTO questions VALUES (
                1, 'A', 'Materiaal', 'Harnas', 'NL', 1,
                'Nederlandse vraag', 'Ja', '[]', 'example-image'
            );
            INSERT INTO questions VALUES (
                2, 'A', 'Equipment', 'Harness', 'EN', 1,
                'English question', 'Yes', '[]', 'example-image'
            );
            """
        )
        conn.commit()
        conn.close()

        try:
            for language, expected_attribution, expected_question in (
                ("NL", "Bron: https://source.example/image", "Nederlandse vraag"),
                ("EN", "Source: https://source.example/image", "English question"),
            ):
                client = app.test_client()
                with client.session_transaction() as session:
                    session.update(
                        {
                            "language": language,
                            "level": "A",
                            "question_amount": 1,
                            "selected_topics": None,
                        }
                    )

                app_module._language_column_cache.clear()
                with patch.object(app_module, "get_db_path", return_value=db_path):
                    response = client.get("/exam")

                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected_question, html)
                expected_label, expected_source = expected_attribution.split(": ", 1)
                self.assertIn(expected_label, html)
                self.assertIn(expected_source, html)
                self.assertIn(
                    '<a href="https://source.example/image" target="_blank" '
                    'rel="noopener noreferrer">https://source.example/image</a>',
                    html,
                )
                self.assertIn("/static/question_images/example.jpg", html)

                with client.session_transaction() as session:
                    question_id = session["exam_question_ids"][0]
                wrong_answer = "Nee" if language == "NL" else "No"
                with patch.object(app_module, "get_db_path", return_value=db_path):
                    result_response = client.post(
                        "/exam",
                        data=csrf_data(
                            client, **{f"answer_{question_id}": wrong_answer}
                        ),
                    )

                result_html = result_response.get_data(as_text=True)
                self.assertEqual(result_response.status_code, 200)
                self.assertIn(expected_label, result_html)
                self.assertIn(expected_source, result_html)
                self.assertIn(
                    '<a href="https://source.example/image" target="_blank" '
                    'rel="noopener noreferrer">https://source.example/image</a>',
                    result_html,
                )
                self.assertIn("/static/question_images/example.jpg", result_html)

            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE image_source SET image_original_source = ?",
                ("Photographer credit",),
            )
            conn.commit()
            conn.close()

            client = app.test_client()
            with client.session_transaction() as session:
                session.update(
                    {
                        "language": "NL",
                        "level": "A",
                        "question_amount": 1,
                        "selected_topics": None,
                    }
                )
            app_module._language_column_cache.clear()
            with patch.object(app_module, "get_db_path", return_value=db_path):
                response = client.get("/exam")

            html = response.get_data(as_text=True)
            self.assertIn("Photographer credit", html)
            self.assertNotIn('href="Photographer credit"', html)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_b_exam_image_assets_all_load(self):
        with app.test_request_context("/"):
            questions = app_module.fetch_questions("B", 64, language="NL")
            image_urls = {
                app_module.render_question(question, "B", "NL")["image_url"]
                for question in questions
            }
        image_urls.discard(None)

        self.assertTrue(image_urls)
        client = app.test_client()
        for image_url in image_urls:
            response = client.get(image_url)
            try:
                self.assertEqual(response.status_code, 200, image_url)
                self.assertTrue(
                    response.content_type.startswith("image/"),
                    (image_url, response.content_type),
                )
            finally:
                response.close()

    def test_level_selection_redirects_to_mode_selection(self):
        client = app.test_client()
        response = client.post(
            "/practice/select",
            data=csrf_data(client, level="A"),
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/practice/mode/A"))

    def test_mode_selection_shows_practice_options(self):
        client = app.test_client()
        response = client.get("/practice/mode/A")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Wil je vrij oefenen, of een oefenexamen maken?", html)
        self.assertIn("Oefenexamen maken", html)
        self.assertIn("Vrij oefenen", html)

    def test_exam_mode_sets_full_exam_question_count(self):
        client = app.test_client()
        response = client.post(
            "/practice/mode/A",
            data=csrf_data(client, practice_type="exam"),
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/exam"))
        with client.session_transaction() as session:
            self.assertEqual(session["level"], "A")
            self.assertEqual(session["question_amount"], 40)

    def test_free_practice_redirects_to_question_amount_prompt(self):
        client = app.test_client()
        response = client.post(
            "/practice/mode/A",
            data=csrf_data(client, practice_type="free"),
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/practice/free/A"))

    def test_free_practice_page_lists_topics_for_selection(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                level TEXT,
                topic TEXT,
                subtopic TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO questions (level, topic, subtopic, is_active, question, true_answer, false_answers) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("A", "Materiaal", "Harnas", 1, "Q1", "Ja", "[]"),
                ("A", "Meteo", "Wind", 1, "Q2", "Ja", "[]"),
            ],
        )
        conn.commit()
        conn.close()

        try:
            with patch.object(app_module, "get_db_path", return_value=db_path):
                response = app.test_client().get("/practice/free/A")

            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Materiaal", html)
            self.assertIn("Meteo", html)
            self.assertIn("selected_topics", html)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_fetch_questions_can_limit_to_selected_topics(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                level TEXT,
                topic TEXT,
                subtopic TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO questions (level, topic, subtopic, is_active, question, true_answer, false_answers) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("A", "Materiaal", "Harnas", 1, "Q1", "Ja", "[]"),
                ("A", "Materiaal", "Harnas", 1, "Q2", "Ja", "[]"),
                ("A", "Meteo", "Wind", 1, "Q3", "Ja", "[]"),
            ],
        )
        conn.commit()
        conn.close()

        try:
            with patch.object(app_module, "get_db_path", return_value=db_path):
                questions = app_module.fetch_questions("A", 5, selected_topics=["Materiaal"])

            self.assertEqual(len(questions), 2)
            self.assertTrue(all(row["topic"] == "Materiaal" for row in questions))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_selection_balances_topic_and_subtopic_hierarchy(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                level TEXT,
                topic TEXT,
                subtopic TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT
            )
            """
        )
        rows = [
            ("A", "Traffic", "Signals", 1, "Q1", "Ja", "[]"),
            ("A", "Traffic", "Signals", 1, "Q2", "Ja", "[]"),
            ("A", "Traffic", "Signals", 1, "Q3", "Ja", "[]"),
            ("A", "Traffic", "Signals", 1, "Q4", "Ja", "[]"),
            ("A", "Traffic", "Rules", 1, "Q5", "Ja", "[]"),
            ("A", "Traffic", "Rules", 1, "Q6", "Ja", "[]"),
            ("A", "Traffic", "Rules", 1, "Q7", "Ja", "[]"),
            ("A", "Traffic", "Rules", 1, "Q8", "Ja", "[]"),
            ("A", "Weather", "Wind", 1, "Q9", "Ja", "[]"),
            ("A", "Weather", "Wind", 1, "Q10", "Ja", "[]"),
            ("A", "Weather", "Wind", 1, "Q11", "Ja", "[]"),
            ("A", "Weather", "Wind", 1, "Q12", "Ja", "[]"),
            ("A", "Weather", "Clouds", 1, "Q13", "Ja", "[]"),
            ("A", "Weather", "Clouds", 1, "Q14", "Ja", "[]"),
            ("A", "Weather", "Clouds", 1, "Q15", "Ja", "[]"),
            ("A", "Weather", "Clouds", 1, "Q16", "Ja", "[]"),
        ]
        conn.executemany(
            "INSERT INTO questions (level, topic, subtopic, is_active, question, true_answer, false_answers) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

        try:
            with patch.object(app_module, "get_db_path", return_value=db_path):
                counts, selected_total = app_module.get_topic_counts_for_level("A", 8)

            self.assertEqual(selected_total, 8)
            self.assertEqual(
                counts,
                {
                    "Traffic": {"Signals": 2, "Rules": 2},
                    "Weather": {"Wind": 2, "Clouds": 2},
                },
            )
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_selection_redistributes_questions_from_topics_at_capacity(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                level TEXT,
                topic TEXT,
                subtopic TEXT,
                is_active INTEGER,
                question TEXT,
                true_answer TEXT,
                false_answers TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO questions
                (level, topic, subtopic, is_active, question, true_answer, false_answers)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("A", "Small", "Only", 1, f"Small {index}", "Ja", "[]")
                for index in range(2)
            ]
            + [
                ("A", "Large", "Only", 1, f"Large {index}", "Ja", "[]")
                for index in range(10)
            ],
        )
        conn.commit()
        conn.close()

        try:
            with patch.object(app_module, "get_db_path", return_value=db_path):
                counts, selected_total = app_module.get_topic_counts_for_level("A", 8)

            self.assertEqual(selected_total, 8)
            self.assertEqual(sum(sum(subtopics.values()) for subtopics in counts.values()), 8)
            self.assertEqual(counts["Small"]["Only"], 2)
            self.assertEqual(counts["Large"]["Only"], 6)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


class ContactPageTests(unittest.TestCase):
    def test_contact_page_shows_message_form(self):
        client = app.test_client()

        response = client.get("/contact")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="email"', html)
        self.assertIn('name="subject"', html)
        self.assertIn('name="message"', html)
        self.assertIn("Vraag over de website", html)

    def test_contact_form_rejects_invalid_input(self):
        client = app.test_client()

        response = client.post(
            "/contact",
            data=csrf_data(client, email="not-an-email", subject="Anders", message="Test"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Vul een geldig e-mailadres in.", response.get_data(as_text=True))

    def test_contact_form_sends_email_and_redirects_to_confirmation(self):
        client = app.test_client()
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        environment = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.test",
            "SMTP_PASSWORD": "secret",
            "SMTP_FROM_EMAIL": "mailer@example.test",
            "CONTACT_EMAIL": "owner@example.test",
            "SMTP_USE_TLS": "1",
        }

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        try:
            with patch.object(app_module, "get_db_path", return_value=db_path), patch.dict(
                os.environ, environment, clear=False
            ), patch.object(app_module.smtplib, "SMTP", return_value=smtp) as smtp_factory, patch.object(
                app_module,
                "start_contact_message_delivery",
                side_effect=lambda **values: app_module.send_contact_message(**values),
            ):
                response = client.post(
                    "/contact",
                    data=csrf_data(
                        client,
                        email="visitor@example.test",
                        subject="Suggestie",
                        message="Een nuttige suggestie.",
                    ),
                )
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

        self.assertEqual(response.status_code, 302)
        self.assertIn("sent=1", response.headers["Location"])
        smtp_factory.assert_called_once_with("smtp.example.test", 587, timeout=20)
        smtp.starttls.assert_called_once_with()
        smtp.login.assert_called_once_with("mailer@example.test", "secret")
        sent_email = smtp.send_message.call_args.args[0]
        self.assertEqual(sent_email["To"], "owner@example.test")
        self.assertEqual(sent_email["Reply-To"], "visitor@example.test")
        self.assertIn("Een nuttige suggestie.", sent_email.get_content())

        confirmation = client.get(response.headers["Location"])
        self.assertIn("Je bericht is verzonden.", confirmation.get_data(as_text=True))

    def test_contact_usage_allows_three_messages_and_cleans_old_rows(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as temp_db:
            db_path = temp_db.name

        try:
            with patch.object(app_module, "get_db_path", return_value=db_path):
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "CREATE TABLE contact_form_usage (date TEXT, ip TEXT)"
                )
                conn.execute(
                    "INSERT INTO contact_form_usage (date, ip) VALUES (datetime('now', '-31 days'), ?)",
                    ("203.0.113.10",),
                )
                conn.executemany(
                    "INSERT INTO contact_form_usage (date, ip) VALUES (datetime('now', '-1 hour'), ?)",
                    [("203.0.113.10",), ("203.0.113.10",)],
                )
                conn.commit()
                conn.close()

                self.assertTrue(
                    app_module.reserve_contact_form_usage("203.0.113.10")
                )
                self.assertFalse(
                    app_module.reserve_contact_form_usage("203.0.113.10")
                )

                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    "SELECT date, ip FROM contact_form_usage ORDER BY date"
                ).fetchall()
                conn.close()

            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row[1] == "203.0.113.10" for row in rows))
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_contact_form_shows_rate_limit_warning_without_sending(self):
        client = app.test_client()

        with patch.object(
            app_module, "reserve_contact_form_usage", return_value=False
        ), patch.object(app_module, "send_contact_message") as send_message:
            response = client.post(
                "/contact",
                data=csrf_data(
                    client,
                    email="visitor@example.test",
                    subject="Suggestie",
                    message="Een nuttige suggestie.",
                ),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("limiet van 3 berichten per dag", response.get_data(as_text=True))
        send_message.assert_not_called()

    def test_get_db_path_resolves_local_db_when_production_db_unavailable(self):
        with patch.dict(os.environ, {}, clear=False):
            if "DB_PATH" in os.environ:
                del os.environ["DB_PATH"]
            db_path = app_module.get_db_path()
            self.assertTrue(db_path.endswith("data.db"))
            self.assertTrue(os.path.isabs(db_path))

    def test_b_practice_flow_succeeds_without_db_path_env(self):
        client = app.test_client()
        with patch.dict(os.environ, {}, clear=False):
            if "DB_PATH" in os.environ:
                del os.environ["DB_PATH"]
            client.post("/practice/select", data=csrf_data(client, level="B"))
            response = client.post("/practice/mode/B", data=csrf_data(client, practice_type="exam"))
            self.assertEqual(response.status_code, 302)
            exam_response = client.get("/exam")
            self.assertEqual(exam_response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
