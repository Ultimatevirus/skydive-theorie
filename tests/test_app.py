import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module
from app import app


class PracticeFlowTests(unittest.TestCase):
    def test_recent_template_strings_are_in_english_translation_map(self):
        required = [
            "Oefenen voor je KNVVL A/B brevet",
            "Let op!: Dit zijn geen officiële KNVVL (oefen)examenvragen!",
            "Op dit moment zijn de vragen nog NIET gevalideerd!",
            "METAR mode",
            "Kies een luchthaven en decodeer de actuele METAR. Let goed op de voorbeeldinvoer voordat je begint. De METAR's worden een keer per dag bijgewerkt.",
            "Selecteer een luchthaven om de dagelijkse METAR te laden.",
        ]

        for key in required:
            self.assertIn(key, app_module.TRANSLATIONS["EN"])

    def test_page_load_does_not_hide_content_before_assets_finish_loading(self):
        transition_script = (Path(app_module.BASE_DIR) / "static" / "page-transition.js").read_text()
        stylesheet = (Path(app_module.BASE_DIR) / "static" / "style.css").read_text()
        client = app.test_client()
        html = client.get("/").get_data(as_text=True)
        background_response = client.get("/static/branding/background.jpg")

        self.assertNotIn("page-entering", transition_script)
        self.assertIn("page-leaving", transition_script)
        self.assertNotIn("page-entering", stylesheet)
        self.assertIn("background-color: #eef6fb", stylesheet)
        self.assertIn('rel="preload" as="image"', html)
        self.assertIn('fetchpriority="high"', html)
        self.assertIn("branding/background.jpg", html)
        self.assertIn("branding/logo.png", html)
        self.assertEqual(background_response.status_code, 200)
        self.assertIn("public", background_response.headers["Cache-Control"])
        self.assertIn("max-age=31536000", background_response.headers["Cache-Control"])

    def test_health_check_is_available_for_reverse_proxy(self):
        response = app.test_client().get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_language_selection_persists_and_localizes_pages(self):
        client = app.test_client()

        response = client.post("/language", data={"language": "EN"})

        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session["language"], "EN")

        response = client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn("Practice exam", html)
        self.assertIn('data-language="EN"', html)
        self.assertIn('language-option is-selected', html)
        self.assertIn('<html lang="en">', html)

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

    def test_practice_page_shows_level_question_and_cards(self):
        client = app.test_client()
        response = client.get("/practice")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Welk brevet wil je voor oefenen?", html)
        self.assertIn("A", html)
        self.assertIn("B", html)

    def test_level_selection_redirects_to_mode_selection(self):
        client = app.test_client()
        response = client.post(
            "/practice/select",
            data={"level": "A"},
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
            data={"practice_type": "exam"},
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
            data={"practice_type": "free"},
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


class ContactFormTests(unittest.TestCase):
    smtp_env = {
        "SMTP_HOST": "smtp.example.com",
        "CONTACT_RECIPIENT_EMAIL": "owner@example.com",
    }

    def test_contact_form_sends_email_on_success(self):
        client = app.test_client()

        with patch.dict(os.environ, self.smtp_env), patch("app.smtplib.SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            response = client.post(
                "/contact",
                data={"name": "Jane", "email": "jane@example.com", "message": "Hello there"},
            )

        smtp.send_message.assert_called_once()
        sent_message = smtp.send_message.call_args[0][0]
        self.assertEqual(sent_message["Reply-To"], "jane@example.com")
        self.assertIn("Je bericht is verzonden", response.get_data(as_text=True))

    def test_contact_form_shows_error_when_smtp_fails(self):
        client = app.test_client()

        with patch.dict(os.environ, self.smtp_env), patch("app.smtplib.SMTP") as smtp_cls:
            smtp_cls.return_value.__enter__.side_effect = OSError("connection refused")
            response = client.post(
                "/contact",
                data={"name": "Jane", "email": "jane@example.com", "message": "Hello there"},
            )

        self.assertIn("Er ging iets mis bij het verzenden", response.get_data(as_text=True))

    def test_contact_form_rejects_header_injection(self):
        client = app.test_client()

        with patch.dict(os.environ, self.smtp_env), patch("app.smtplib.SMTP") as smtp_cls:
            response = client.post(
                "/contact",
                data={"name": "Jane\r\nBcc: attacker@example.com", "email": "jane@example.com", "message": "Hello"},
            )

        smtp_cls.return_value.__enter__.return_value.send_message.assert_not_called()
        self.assertIn("Er ging iets mis bij het verzenden", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
