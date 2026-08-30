import unittest

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


if __name__ == "__main__":
    unittest.main()
