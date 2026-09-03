"""Pure quiz answer, scoring, and grouping helpers."""

import ast
import random


ALLOWED_LANGUAGES = ("NL", "EN")


def normalize_language(language):
    """Return a validated language code or the Dutch default."""
    normalized = str(language or "NL").strip().upper()
    return normalized if normalized in ALLOWED_LANGUAGES else "NL"


def normalize_answer_value(value):
    """Normalize a submitted or stored answer to a consistent lowercase value."""
    if value is None:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    lowered = text.lower()

    if lowered in ("ja", "j", "yes", "y", "true", "waar", "1"):
        return "ja"
    if lowered in ("nee", "n", "no", "false", "onwaar", "0"):
        return "nee"

    return text.lower()


def display_answer_value(value, language="NL"):
    """Convert a normalized answer into a user-friendly display label."""
    normalized = normalize_answer_value(value)

    if normalized == "ja":
        return "Yes" if normalize_language(language) == "EN" else "Ja"
    if normalized == "nee":
        return "No" if normalize_language(language) == "EN" else "Nee"

    return str(value).strip()


def normalize_options(raw_options):
    """Return a clean list of option strings from varying stored formats."""
    if raw_options is None:
        return []

    if isinstance(raw_options, (list, tuple, set)):
        return [str(item).strip() for item in raw_options if str(item).strip()]

    if isinstance(raw_options, str):
        text = raw_options.strip()
        if not text:
            return []

        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
                if isinstance(parsed, (list, tuple, set)):
                    return [
                        str(item).strip()
                        for item in parsed
                        if str(item).strip()
                    ]
            except (ValueError, SyntaxError):
                pass

            text = text[1:-1].strip()

        if not text:
            return []

        return [
            item.strip().strip("'\"")
            for item in text.split(",")
            if item.strip()
        ]

    return [str(raw_options)]


def build_option_list(false_options, correct_answer, level="B", language="NL"):
    """Build a shuffled multiple-choice answer map for a given exam level."""
    correct_answer = normalize_answer_value(correct_answer)
    wrong_answers = []

    for item in normalize_options(false_options):
        normalized = normalize_answer_value(item)
        if normalized and normalized != correct_answer:
            wrong_answers.append(normalized)

    wrong_answers = list(dict.fromkeys(wrong_answers))
    if len(wrong_answers) > 3:
        wrong_answers = random.sample(wrong_answers, 3)

    options = wrong_answers + [correct_answer]
    random.shuffle(options)

    if level == "A":
        option_map = {
            "A": display_answer_value(options[0], language),
            "B": (
                display_answer_value(options[1], language)
                if len(options) > 1
                else display_answer_value(options[0], language)
            ),
        }
        if normalize_answer_value(option_map["A"]) == "ja":
            option_map = {"A": "Ja", "B": "Nee"}
        elif normalize_answer_value(option_map["B"]) == "ja":
            option_map = {"A": "Nee", "B": "Ja"}
        return option_map

    labels = ["A", "B", "C", "D"]
    return {
        labels[index]: display_answer_value(option, language)
        for index, option in enumerate(options[:4])
    }


def normalize_selected_topics(raw_topics):
    """Return a deduplicated list of selected topic names."""
    if raw_topics is None:
        return []

    if isinstance(raw_topics, str):
        raw_values = [raw_topics]
    else:
        raw_values = list(raw_topics)

    cleaned = []
    for value in raw_values:
        if value is None:
            continue
        name = str(value).strip()
        if name and name not in cleaned:
            cleaned.append(name)

    return cleaned


def format_elapsed(seconds):
    """Convert elapsed seconds into a HH:MM:SS or MM:SS display string."""
    total_seconds = int(seconds)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def calculate_grade(score, total):
    """Return the final grade on a 10-point scale based on score ratio."""
    if total == 0:
        return 0.0
    return round((score / total) * 10, 1)


def is_passed(score, total):
    """Check whether the overall exam score passes the 60% threshold."""
    if total == 0:
        return False
    return (score / total) * 100 >= 60


def topic_passed(correct, total):
    """Check whether a topic score passes the 60% threshold."""
    if total == 0:
        return False
    return (correct / total) * 100 >= 60


def build_question_groups(questions):
    """Group rendered questions by topic for topic-based exam UI."""
    grouped = {}
    for question in questions:
        topic = question.get("topic") or "Onbekend"
        grouped.setdefault(topic, []).append(question)

    return [
        {"topic": topic, "questions": topic_questions}
        for topic, topic_questions in sorted(grouped.items())
    ]
