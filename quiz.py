"""Pure quiz answer, scoring, and grouping helpers."""

import ast
import random


ALLOWED_LANGUAGES = ("NL", "EN")


def normalize_language(language):
    """Return a validated language code or the Dutch default."""
    normalized = str(language or "NL").strip().upper()
    return normalized if normalized in ALLOWED_LANGUAGES else "NL"


def normalize_answer_value(value):
    """Normalize a submitted or stored answer to a consistent lowercase key for comparison."""
    if value is None:
        return ""

    text = str(value).strip()
    if not text:
        return ""

    lowered = text.lower()

    # Only exact yes/no words are treated as boolean answers, so numeric or
    # abbreviated answers (e.g. "1", "0", "j") used in multiple-choice
    # questions are never mistaken for a Ja/Nee answer.
    if lowered in ("ja", "yes"):
        return "ja"
    if lowered in ("nee", "no"):
        return "nee"

    return lowered


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
    correct_display = str(correct_answer).strip() if correct_answer is not None else ""
    correct_key = normalize_answer_value(correct_answer)

    # Keep the original casing of each option for display, only using the
    # normalized key to dedupe and to exclude the correct answer.
    wrong_answers = []
    seen_keys = {correct_key}
    for item in normalize_options(false_options):
        key = normalize_answer_value(item)
        if key and key not in seen_keys:
            wrong_answers.append(item)
            seen_keys.add(key)

    if len(wrong_answers) > 3:
        wrong_answers = random.sample(wrong_answers, 3)

    options = wrong_answers + [correct_display]
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
            option_map = {
                "A": display_answer_value("ja", language),
                "B": display_answer_value("nee", language),
            }
        elif normalize_answer_value(option_map["B"]) == "ja":
            option_map = {
                "A": display_answer_value("nee", language),
                "B": display_answer_value("ja", language),
            }
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
