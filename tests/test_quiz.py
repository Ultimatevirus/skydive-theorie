import unittest

from quiz import (
    build_question_groups,
    calculate_grade,
    format_elapsed,
    is_passed,
    normalize_answer_value,
    normalize_options,
    normalize_selected_topics,
    topic_passed,
)


class QuizHelperTests(unittest.TestCase):
    def test_normalize_answer_value_handles_boolean_variants(self):
        self.assertEqual(normalize_answer_value(" YES "), "ja")
        self.assertEqual(normalize_answer_value("onwaar"), "nee")
        self.assertEqual(normalize_answer_value("  42 "), "42")

    def test_normalize_options_supports_stored_list_formats(self):
        self.assertEqual(normalize_options("['A', 'B']"), ["A", "B"])
        self.assertEqual(normalize_options("A, B"), ["A", "B"])
        self.assertEqual(normalize_options(None), [])

    def test_topic_selection_is_cleaned_and_deduplicated(self):
        self.assertEqual(
            normalize_selected_topics([" Meteo ", "Meteo", None, ""]),
            ["Meteo"],
        )

    def test_scoring_helpers_handle_empty_and_boundary_scores(self):
        self.assertEqual(calculate_grade(0, 0), 0.0)
        self.assertTrue(is_passed(3, 5))
        self.assertFalse(is_passed(2, 5))
        self.assertTrue(topic_passed(3, 5))
        self.assertFalse(topic_passed(0, 0))

    def test_elapsed_time_and_question_groups_are_presentational(self):
        self.assertEqual(format_elapsed(3661), "01:01:01")
        self.assertEqual(
            build_question_groups(
                [{"topic": "Meteo", "id": 1}, {"topic": None, "id": 2}]
            ),
            [
                {"topic": "Meteo", "questions": [{"topic": "Meteo", "id": 1}]},
                {"topic": "Onbekend", "questions": [{"topic": None, "id": 2}]},
            ],
        )


if __name__ == "__main__":
    unittest.main()
