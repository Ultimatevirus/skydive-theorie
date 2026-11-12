"""Contact email delivery and per-client submission limits."""

from contextlib import closing
from email.message import EmailMessage
import os
import smtplib


_CONTACT_TABLE_READY = set()


def send_contact_message(sender_email, subject, message):
    """Send a contact message using SMTP settings supplied through the environment."""
    smtp_host = os.getenv("SMTP_HOST")
    recipient = os.getenv("CONTACT_EMAIL")
    if not smtp_host or not recipient:
        raise RuntimeError("SMTP_HOST and CONTACT_EMAIL must be configured")

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM_EMAIL") or smtp_username or recipient
    use_ssl = os.getenv("SMTP_USE_SSL", "0").lower() in {"1", "true", "yes"}
    use_tls = os.getenv("SMTP_USE_TLS", "1").lower() in {"1", "true", "yes"}

    email = EmailMessage()
    email["From"] = sender
    email["To"] = recipient
    email["Reply-To"] = sender_email
    email["Subject"] = f"Contactformulier: {subject}"
    email.set_content(f"Afzender: {sender_email}\nOnderwerp: {subject}\n\n{message}")

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(smtp_host, smtp_port, timeout=20) as smtp:
        if use_tls and not use_ssl:
            smtp.starttls()
        if smtp_username:
            smtp.login(smtp_username, smtp_password or "")
        smtp.send_message(email)


def reserve_contact_form_usage(connection_factory, database_path, ip_address):
    """Reserve one contact message for an IP address within the daily limit."""
    path = database_path()
    with closing(connection_factory()) as conn:
        if path not in _CONTACT_TABLE_READY or not os.path.exists(path):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contact_form_usage (
                    date TEXT,
                    ip TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS contact_form_usage_ip_date
                ON contact_form_usage (ip, date)
                """
            )
            conn.commit()
            _CONTACT_TABLE_READY.add(path)

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM contact_form_usage WHERE date < datetime('now', '-30 days')"
            )
            usage_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM contact_form_usage
                WHERE ip = ? AND date >= datetime('now', '-24 hours')
                """,
                (ip_address,),
            ).fetchone()[0]
            if usage_count >= 3:
                conn.commit()
                return False

            conn.execute(
                "INSERT INTO contact_form_usage (date, ip) VALUES (datetime('now'), ?)",
                (ip_address,),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
