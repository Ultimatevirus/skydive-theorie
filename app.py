import ast
import os
import random
import sqlite3
import time
from pathlib import Path

from flask import (
    Flask,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = "skydive-secret"

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOCAL_DB = BASE_DIR / "vragen.db"
PRODUCTION_DB = Path("/data/vragen.db")


def get_db_path():
    """Resolve a stable SQLite database path for local and container use."""
    configured_path = os.getenv("DB_PATH")
    if configured_path:
        db_path = Path(configured_path)
    elif PRODUCTION_DB.exists():
        db_path = PRODUCTION_DB
    elif DEFAULT_LOCAL_DB.exists():
        db_path = DEFAULT_LOCAL_DB
        if not PRODUCTION_DB.exists():
            PRODUCTION_DB.parent.mkdir(parents=True, exist_ok=True)
            try:
                PRODUCTION_DB.write_bytes(DEFAULT_LOCAL_DB.read_bytes())
            except OSError:
                pass
        db_path = PRODUCTION_DB
    else:
        db_path = PRODUCTION_DB

    db_path.parent.mkdir(parents=True, exist_ok=True)
    return str(db_path)


def get_db_connection():
    """Create and return a SQLite database connection for the quiz data."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


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


def display_answer_value(value):
    """Convert a normalized answer into a user-friendly display label."""
    normalized = normalize_answer_value(value)

    if normalized == "ja":
        return "Ja"
    if normalized == "nee":
        return "Nee"

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


def build_option_list(false_options, correct_answer, level="B"):
    """Build a shuffled multiple-choice answer map for a given exam level."""
    correct_answer = normalize_answer_value(correct_answer)

    wrong_answers = normalize_options(false_options)
    wrong_answers = [
        normalize_answer_value(item)
        for item in wrong_answers
        if normalize_answer_value(item)
        and normalize_answer_value(item) != correct_answer
    ]
    wrong_answers = list(dict.fromkeys(wrong_answers))

    if len(wrong_answers) > 3:
        wrong_answers = random.sample(wrong_answers, 3)

    options = wrong_answers + [correct_answer]
    random.shuffle(options)

    if level == "A":
        option_map = {
            "A": display_answer_value(options[0]),
            "B": (
                display_answer_value(options[1])
                if len(options) > 1
                else display_answer_value(options[0])
            ),
        }
        if normalize_answer_value(option_map["A"]) == "ja":
            option_map = {"A": "Ja", "B": "Nee"}
        elif normalize_answer_value(option_map["B"]) == "ja":
            option_map = {"A": "Nee", "B": "Ja"}
        return option_map

    labels = ["A", "B", "C", "D"]
    option_map = {}
    for index, option in enumerate(options[:4]):
        option_map[labels[index]] = display_answer_value(option)

    return option_map


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


def get_available_topics(level):
    """Return all active topics for the selected exam level."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT DISTINCT topic
        FROM questions
        WHERE level = ? AND is_active = 1
        ORDER BY topic ASC
        """,
        (level,),
    ).fetchall()
    conn.close()
    return [row["topic"] for row in rows]


def get_topic_counts_for_level(level, amount, selected_topics=None):
    """Return per-topic, per-subtopic counts and the total selected questions."""
    selected_topics = normalize_selected_topics(selected_topics)
    conn = get_db_connection()

    if selected_topics:
        placeholders = ", ".join("?" for _ in selected_topics)
        topic_rows = conn.execute(
            f"""
            SELECT topic, subtopic, COUNT(*) AS c
            FROM questions
            WHERE level = ? AND topic IN ({placeholders}) AND is_active = 1
            GROUP BY topic, subtopic
            ORDER BY topic, subtopic
            """,
            (level, *selected_topics),
        ).fetchall()
    else:
        topic_rows = conn.execute(
            """
            SELECT topic, subtopic, COUNT(*) AS c
            FROM questions
            WHERE level = ? AND is_active = 1
            GROUP BY topic, subtopic
            ORDER BY topic, subtopic
            """,
            (level,),
        ).fetchall()
    conn.close()

    if not topic_rows:
        return {}, 0

    available_by_topic = {}
    for row in topic_rows:
        topic = row["topic"]
        subtopic = row["subtopic"]
        available_by_topic.setdefault(topic, {})
        available_by_topic[topic][subtopic] = row["c"]

    topic_names = sorted(available_by_topic)
    total_available = sum(
        sum(subtopic_counts.values())
        for subtopic_counts in available_by_topic.values()
    )
    selected_total = min(amount, total_available)

    if selected_total <= 0:
        empty_counts = {
            topic: {subtopic: 0 for subtopic in sorted(subtopics)}
            for topic, subtopics in available_by_topic.items()
        }
        return empty_counts, 0

    counts = {
        topic: {subtopic: 0 for subtopic in sorted(subtopics)}
        for topic, subtopics in available_by_topic.items()
    }

    topic_targets = {topic: 0 for topic in topic_names}
    base = selected_total // len(topic_names)
    remainder = selected_total % len(topic_names)
    for index, topic in enumerate(topic_names):
        topic_total = sum(available_by_topic[topic].values())
        topic_targets[topic] = min(
            base + (1 if index < remainder else 0),
            topic_total,
        )

    used = sum(topic_targets.values())
    if used < selected_total:
        for topic in topic_names:
            free_space = sum(available_by_topic[topic].values()) - sum(
                counts[topic].values()
            )
            if free_space > 0:
                extra = min(selected_total - used, free_space)
                topic_targets[topic] += extra
                used += extra
                if used >= selected_total:
                    break

    for topic in topic_names:
        target = topic_targets.get(topic, 0)
        if target <= 0:
            continue

        subtopics = sorted(available_by_topic[topic])
        subtopic_base = target // len(subtopics)
        subtopic_remainder = target % len(subtopics)

        for index, subtopic in enumerate(subtopics):
            available = available_by_topic[topic].get(subtopic, 0)
            counts[topic][subtopic] = min(
                subtopic_base + (1 if index < subtopic_remainder else 0),
                available,
            )

        topic_used = sum(counts[topic].values())
        if topic_used < target:
            for subtopic in subtopics:
                free_space = available_by_topic[topic].get(subtopic, 0) - counts[topic].get(subtopic, 0)
                if free_space > 0:
                    extra = min(target - topic_used, free_space)
                    counts[topic][subtopic] += extra
                    topic_used += extra
                    if topic_used >= target:
                        break

    return counts, selected_total


def fetch_questions(level, amount, selected_topics=None):
    """Fetch a random set of active questions for a level and optional topic filter."""
    selected_topics = normalize_selected_topics(selected_topics)
    topic_counts, selected_total = get_topic_counts_for_level(level, amount, selected_topics)

    if selected_total == 0:
        return []

    conn = get_db_connection()
    rows = []

    for topic, subtopic_counts in topic_counts.items():
        for subtopic, count in subtopic_counts.items():
            if count <= 0:
                continue

            subtopic_rows = conn.execute(
                """
                SELECT rowid AS id, *
                FROM questions
                WHERE level = ? AND topic = ? AND subtopic = ? AND is_active = 1
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (level, topic, subtopic, count),
            ).fetchall()

            rows.extend(subtopic_rows)

    conn.close()

    random.shuffle(rows)
    return rows


def fetch_questions_by_ids(level, question_ids):
    """Fetch active questions by ID for a specific level."""
    normalized_ids = [
        int(question_id) for question_id in question_ids if str(question_id).strip()
    ]
    if not normalized_ids:
        return []

    placeholders = ", ".join("?" for _ in normalized_ids)
    conn = get_db_connection()
    rows = conn.execute(
        f"""
        SELECT rowid AS id, *
        FROM questions
        WHERE level = ? AND rowid IN ({placeholders}) AND is_active = 1
        """,
        (level, *normalized_ids),
    ).fetchall()
    conn.close()

    row_map = {int(row["id"]): row for row in rows}
    return [
        row_map[question_id]
        for question_id in normalized_ids
        if question_id in row_map
    ]


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


@app.route("/favicon.svg")
@app.route("/favicon.ico")
def favicon():
    """Serve a high-contrast favicon for the browser tab."""
    icon_path = os.path.join(app.root_path, "static", "icons", "plane-favicon.svg")
    response = send_file(icon_path, mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.route("/")
def index():
    """Render the home page."""
    return render_template("index.html")


def start_exam(level, question_amount, selected_topics=None):
    """Set the selected level, question count, and retained topic filters."""
    session["level"] = str(level).strip().upper()
    session["question_amount"] = int(question_amount)
    session["selected_topics"] = normalize_selected_topics(selected_topics)
    session["started_at"] = time.time()
    session["exam_question_ids"] = []


@app.route("/practice")
def practice():
    """Render the first practice selection step: choose the brevet level."""
    return render_template(
        "practice.html",
        stage="level",
        question_text="Welk brevet wil je voor oefenen?",
        level=None,
        error=None,
    )


@app.route("/practice/select", methods=["POST"])
def practice_select():
    """Store the chosen brevet and continue to the mode selection step."""
    level = request.form.get("level", "").strip().upper()
    if level not in ("A", "B"):
        return "Ongeldige keuze. Kies A of B.", 400

    session["selected_level"] = level
    return redirect(url_for("practice_mode", level=level))


@app.route("/practice/mode/<level>", methods=["GET", "POST"])
def practice_mode(level):
    """Choose whether to create a full practice exam or do free practice."""
    normalized_level = str(level).strip().upper()
    if normalized_level not in ("A", "B"):
        return "Ongeldige keuze. Kies A of B.", 400

    if request.method == "POST":
        practice_type = request.form.get("practice_type", "").strip().lower()
        if practice_type == "exam":
            start_exam(normalized_level, 40)
            return redirect(url_for("exam"))
        if practice_type == "free":
            return redirect(url_for("practice_free", level=normalized_level))
        return "Ongeldige keuze. Kies een oefenmodus.", 400

    session["selected_level"] = normalized_level
    return render_template(
        "practice.html",
        stage="mode",
        level=normalized_level,
        question_text="Wil je vrij oefenen, of een oefenexamen maken?",
        error=None,
    )


@app.route("/practice/free/<level>", methods=["GET", "POST"])
def practice_free(level):
    """Prompt for a custom question count and start free practice."""
    normalized_level = str(level).strip().upper()
    if normalized_level not in ("A", "B"):
        return "Ongeldige keuze. Kies A of B.", 400

    available_topics = get_available_topics(normalized_level)

    if request.method == "POST":
        try:
            question_amount = int(request.form.get("question_amount", "1"))
        except ValueError:
            question_amount = 1

        selected_topics = normalize_selected_topics(request.form.getlist("selected_topics"))
        if not selected_topics:
            selected_topics = available_topics

        if not 1 <= question_amount <= 40:
            return render_template(
                "practice.html",
                stage="free",
                level=normalized_level,
                question_text="Hoeveel vragen wil je laden?",
                error="Kies een getal tussen 1 en 40.",
                available_topics=available_topics,
                selected_topics=selected_topics,
            )

        if not available_topics:
            return render_template(
                "practice.html",
                stage="free",
                level=normalized_level,
                question_text="Hoeveel vragen wil je laden?",
                error="Er zijn nog geen vragen beschikbaar voor dit brevet.",
                available_topics=[],
                selected_topics=[],
            )

        start_exam(normalized_level, question_amount, selected_topics)
        return redirect(url_for("exam"))

    default_selected_topics = available_topics
    return render_template(
        "practice.html",
        stage="free",
        level=normalized_level,
        question_text="Hoeveel vragen wil je laden?",
        error=None,
        available_topics=available_topics,
        selected_topics=default_selected_topics,
    )


@app.route("/leren")
def learn():
    """Render the learning page."""
    return render_template("learn.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    """Handle contact form display and validation."""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()

        if not name or not email or not message:
            # Template context used when the contact form contains missing values.
            contact_context = {
                "success": False,
                "error": "Vul alle velden in om contact op te nemen.",
                "name": name,
            }
            return render_template("contact.html", **contact_context)

        # Template context used after a successful contact form submission.
        contact_context = {
            "success": True,
            "error": None,
            "name": name,
        }
        return render_template("contact.html", **contact_context)

    # Template context used when the contact page is first opened.
    contact_context = {
        "success": False,
        "error": None,
        "name": "",
    }
    return render_template("contact.html", **contact_context)


@app.route("/start", methods=["POST"])
def start():
    """Validate the selected exam settings and redirect to the exam page."""
    level = request.form.get("level", "").strip().upper()
    try:
        question_amount = int(request.form.get("question_amount", "1"))
    except ValueError:
        question_amount = 1

    if level not in ("A", "B"):
        return "Ongeldige keuze. Kies A of B.", 400

    if not 1 <= question_amount <= 40:
        return "Kies een getal tussen 1 en 40.", 400

    start_exam(level, question_amount)
    return redirect(url_for("exam"))


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


@app.route("/exam", methods=["GET", "POST"])
def exam():
    """Render exam questions, handle submissions, and show the results."""
    level = session.get("level")
    amount = session.get("question_amount", 1)
    selected_topics = session.get("selected_topics")

    if not level:
        return redirect(url_for("index"))

    if request.method == "POST":
        stored_ids = session.get("exam_question_ids", [])
        questions = (
            fetch_questions_by_ids(level, stored_ids)
            if stored_ids
            else fetch_questions(level, amount, selected_topics=selected_topics)
        )
    else:
        questions = fetch_questions(level, amount, selected_topics=selected_topics)

    if request.method == "GET" and questions:
        session["exam_question_ids"] = [row["id"] for row in questions]

    if request.method == "POST":
        rendered_questions = []
        for row in questions:
            correct_answer = normalize_answer_value(row["true_answer"])
            option_map = build_option_list(
                row["false_answers"],
                correct_answer,
                level=level,
            )
            rendered_questions.append(
                {
                    "id": row["id"],
                    "question": row["question"],
                    "topic": row["topic"],
                    "options": option_map,
                    "is_yes_no": level == "A",
                    "image_url": get_question_image_url(
                        get_row_value(row, "image_link")
                    ),
                }
            )

        submitted_ids = {
            key.split("_", 1)[1]
            for key, value in request.form.items()
            if key.startswith("answer_") and str(value).strip()
        }

        missing_answers = [
            str(row["id"])
            for row in questions
            if str(row["id"]) not in submitted_ids
        ]

        if missing_answers:
            # Template context shown when the learner has not answered every exam.
            exam_context = {
                "questions": rendered_questions,
                "question_groups": build_question_groups(rendered_questions),
                "total_questions": len(rendered_questions),
                "started_at": session.get("started_at", time.time()),
                "level": level,
                "error": (
                    "Niet alle vragen zijn ingevuld. Kies voor elke vraag een "
                    "antwoord voordat je verder gaat."
                ),
            }
            return render_template("exam.html", **exam_context)

        started_at = session.get("started_at")
        elapsed_seconds = time.time() - started_at if started_at else 0

        score = 0
        topic_scores = {}
        results = []

        for row in questions:
            topic = row["topic"]
            if topic not in topic_scores:
                topic_scores[topic] = {"correct": 0, "total": 0}

            correct_answer = normalize_answer_value(row["true_answer"])
            option_map = build_option_list(
                row["false_answers"],
                correct_answer,
                level=level,
            )

            submitted_value = request.form.get(f"answer_{row['id']}", "")
            selected_value = normalize_answer_value(submitted_value)

            is_correct = selected_value == correct_answer
            if is_correct:
                score += 1
                topic_scores[topic]["correct"] += 1

            topic_scores[topic]["total"] += 1

            results.append(
                {
                    "question": row["question"],
                    "topic": topic,
                    "selected": display_answer_value(selected_value),
                    "correct": display_answer_value(correct_answer),
                    "is_correct": is_correct,
                    "options": option_map,
                    "image_url": get_question_image_url(
                        get_row_value(row, "image_link")
                    ),
                }
            )

        sorted_topic_scores = {
            topic: {
                **stats,
                "passed": topic_passed(stats["correct"], stats["total"]),
                "percent": (
                    round((stats["correct"] / stats["total"] * 100), 1)
                    if stats["total"]
                    else 0
                ),
            }
            for topic, stats in sorted(topic_scores.items())
        }

        total_questions = len(questions)
        overall_pass = is_passed(score, total_questions) and all(
            detail["passed"] for detail in sorted_topic_scores.values()
        )

        final_grade = calculate_grade(score, total_questions)

        # Template context used for the final exam results page.
        result_context = {
            "score": score,
            "total": total_questions,
            "results": results,
            "elapsed_time": format_elapsed(elapsed_seconds),
            "topic_scores": sorted_topic_scores,
            "grade": final_grade,
            "passed": overall_pass,
        }
        return render_template("result.html", **result_context)

    rendered_questions = []
    for row in questions:
        correct_answer = normalize_answer_value(row["true_answer"])
        option_map = build_option_list(
            row["false_answers"],
            correct_answer,
            level=level,
        )
        rendered_questions.append(
            {
                "id": row["id"],
                "question": row["question"],
                "topic": row["topic"],
                "options": option_map,
                "is_yes_no": level == "A",
                "image_url": get_question_image_url(get_row_value(row, "image_link")),
            }
        )

    # Template context used to render the exam page with the selected questions.
    exam_context = {
        "questions": rendered_questions,
        "question_groups": build_question_groups(rendered_questions),
        "total_questions": len(rendered_questions),
        "started_at": session.get("started_at", time.time()),
        "level": level,
    }
    return render_template("exam.html", **exam_context)


def get_row_value(row, key, default=None):
    """Safely return a value from a row dictionary or default if missing."""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def get_question_image_url(image_link):
    """Convert a stored image path into a static file URL when possible."""
    if image_link is None:
        return None

    image_path = str(image_link).strip()
    if not image_path:
        return None

    normalized = image_path.replace("\\", "/")
    lower = normalized.lower()

    if not (
        lower.endswith(".jpg")
        or lower.endswith(".jpeg")
        or lower.endswith(".png")
    ):
        return None

    if normalized.startswith(("http://", "https://")):
        return normalized

    if normalized.startswith("/"):
        if normalized.startswith("/static/"):
            return normalized
        return url_for("static", filename=normalized.lstrip("/"))

    if normalized.startswith("static/"):
        return url_for("static", filename=normalized.replace("static/", "", 1))

    if normalized.startswith("./"):
        return url_for("static", filename=normalized.lstrip("./"))

    if normalized.startswith("images/") or normalized.startswith("question_images/"):
        return url_for("static", filename=normalized)

    return None

#purely for local development, gunicorn will be used in production
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
