"""Persistent access-gate rate limiting."""

import os
import time
from contextlib import closing


GATE_MAX_FAILURES = 5
GATE_FAILURE_WINDOW = 60
_GATE_TABLE_READY = set()


def ensure_gate_failure_table(conn, database_path):
    """Create the shared failed-attempt table once per database path."""
    path = database_path()
    if path in _GATE_TABLE_READY and os.path.exists(path):
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gate_failure (ip TEXT NOT NULL, failed_at REAL NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS gate_failure_ip ON gate_failure (ip, failed_at)"
    )
    conn.commit()
    _GATE_TABLE_READY.add(path)


def gate_attempts_exhausted(connection_factory, database_path, ip_address):
    """Check whether an IP exceeded the recent failed-attempt limit."""
    cutoff = time.time() - GATE_FAILURE_WINDOW
    with closing(connection_factory()) as conn:
        ensure_gate_failure_table(conn, database_path)
        conn.execute("DELETE FROM gate_failure WHERE failed_at < ?", (cutoff,))
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM gate_failure WHERE ip = ? AND failed_at >= ?",
            (ip_address, cutoff),
        ).fetchone()[0]
        return count >= GATE_MAX_FAILURES


def record_gate_failure(connection_factory, database_path, ip_address):
    """Persist a failed access-code attempt."""
    with closing(connection_factory()) as conn:
        ensure_gate_failure_table(conn, database_path)
        conn.execute(
            "INSERT INTO gate_failure (ip, failed_at) VALUES (?, ?)",
            (ip_address, time.time()),
        )
        conn.commit()
