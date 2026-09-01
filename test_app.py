import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
from app import app


class PracticeFlowTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
