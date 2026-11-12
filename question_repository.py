"""Database access for question selection and exam content."""

import os
import random
import sqlite3
from contextlib import closing

from quiz import distribute_question_counts, normalize_language, normalize_selected_topics


_LANGUAGE_COLUMN_CACHE = {}


class QuestionRepository:
    """Read quiz questions while accommodating older database schemas."""

    def __init__(self, connection_factory, database_path):
        self._connection_factory = connection_factory
        self._database_path = database_path

    def _database_signature(self):
        try:
            stat = os.stat(self._database_path())
        except OSError:
            return None
        return self._database_path(), stat.st_mtime_ns, stat.st_size

    def questions_have_language_column(self, conn):
        signature = self._database_signature()
        if signature is not None and signature in _LANGUAGE_COLUMN_CACHE:
            return _LANGUAGE_COLUMN_CACHE[signature]

        columns = conn.execute("PRAGMA table_info(questions)").fetchall()
        has_column = any(column[1] == "language" for column in columns)
        if signature is not None:
            _LANGUAGE_COLUMN_CACHE.clear()
            _LANGUAGE_COLUMN_CACHE[signature] = has_column
        return has_column

    def language_filter(self, conn, language, prefix=""):
        if self.questions_have_language_column(conn):
            return f" AND {prefix}language = ?", [normalize_language(language)]
        return "", []

    @staticmethod
    def question_query_schema(conn):
        question_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(questions)").fetchall()
        }
        has_image_sources = (
            "image_key" in question_columns
            and conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'image_source'"
            ).fetchone()
            is not None
        )
        if has_image_sources:
            return (
                "questions.rowid AS id, questions.*, "
                "image_source.image_local_source AS image_local_source, "
                "image_source.image_original_source AS image_original_source",
                "questions LEFT JOIN image_source "
                "ON image_source.image_key = questions.image_key",
                "questions.",
            )
        return "rowid AS id, *", "questions", ""

    def get_available_topics(self, level, language="NL"):
        with closing(self._connection_factory()) as conn:
            language_clause, language_params = self.language_filter(conn, language)
            rows = conn.execute(
                f"""
                SELECT DISTINCT topic
                FROM questions
                WHERE level = ? AND is_active = 1{language_clause}
                ORDER BY topic ASC
                """,
                (level, *language_params),
            ).fetchall()
        return [row["topic"] for row in rows]

    def get_topic_counts_for_level(
        self, level, amount, selected_topics=None, language="NL"
    ):
        selected_topics = normalize_selected_topics(selected_topics)
        with closing(self._connection_factory()) as conn:
            language_clause, language_params = self.language_filter(conn, language)
            topic_clause = ""
            topic_params = ()
            if selected_topics:
                placeholders = ", ".join("?" for _ in selected_topics)
                topic_clause = f" AND topic IN ({placeholders})"
                topic_params = tuple(selected_topics)
            topic_rows = conn.execute(
                f"""
                SELECT topic, subtopic, COUNT(*) AS c
                FROM questions
                WHERE level = ?{topic_clause} AND is_active = 1{language_clause}
                GROUP BY topic, subtopic
                ORDER BY topic, subtopic
                """,
                (level, *topic_params, *language_params),
            ).fetchall()

        available_by_topic = {}
        for row in topic_rows:
            available_by_topic.setdefault(row["topic"], {})[row["subtopic"]] = row["c"]
        return distribute_question_counts(available_by_topic, amount)

    def fetch_questions(self, level, amount, selected_topics=None, language="NL"):
        selected_topics = normalize_selected_topics(selected_topics)
        topic_counts, selected_total = self.get_topic_counts_for_level(
            level, amount, selected_topics, language
        )
        if selected_total == 0:
            return []

        with closing(self._connection_factory()) as conn:
            language_clause, language_params = self.language_filter(
                conn, language, prefix="questions."
            )
            select_columns, question_source, column_prefix = self.question_query_schema(
                conn
            )
            topic_names = list(topic_counts)
            placeholders = ", ".join("?" for _ in topic_names)
            rows = conn.execute(
                f"""
                SELECT {select_columns}
                FROM {question_source}
                WHERE {column_prefix}level = ?
                    AND {column_prefix}topic IN ({placeholders})
                    AND {column_prefix}is_active = 1{language_clause}
                """,
                (level, *topic_names, *language_params),
            ).fetchall()

        rows_by_subtopic = {}
        for row in rows:
            key = (row["topic"], row["subtopic"])
            rows_by_subtopic.setdefault(key, []).append(row)

        selected_rows = []
        for topic, subtopic_counts in topic_counts.items():
            for subtopic, count in subtopic_counts.items():
                if count <= 0:
                    continue
                candidates = rows_by_subtopic.get((topic, subtopic), [])
                selected_rows.extend(random.sample(candidates, min(count, len(candidates))))
        random.shuffle(selected_rows)
        return selected_rows

    def fetch_questions_by_ids(self, level, question_ids, language="NL"):
        normalized_ids = [
            int(question_id) for question_id in question_ids if str(question_id).strip()
        ]
        if not normalized_ids:
            return []

        with closing(self._connection_factory()) as conn:
            language_clause, language_params = self.language_filter(
                conn, language, prefix="questions."
            )
            select_columns, question_source, column_prefix = self.question_query_schema(
                conn
            )
            placeholders = ", ".join("?" for _ in normalized_ids)
            rows = conn.execute(
                f"""
                SELECT {select_columns}
                FROM {question_source}
                WHERE {column_prefix}level = ?
                    AND {column_prefix}rowid IN ({placeholders})
                    AND {column_prefix}is_active = 1{language_clause}
                """,
                (level, *normalized_ids, *language_params),
            ).fetchall()

        row_map = {int(row["id"]): row for row in rows}
        return [row_map[item] for item in normalized_ids if item in row_map]
