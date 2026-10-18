"""Persisted checks for external services shown on the public status page."""

from datetime import datetime, timezone
import logging
import os
import smtplib
import sqlite3
import ssl
import time
import uuid
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from db import connect_db, get_db_path


STATUS_CHECK_INTERVAL = 300
OPENDATA_PROBE_INTERVAL = 900
STATUS_MONITOR_INTERVAL = 60
STATUS_PROBE_TIMEOUT = 10
MQTT_ACTIVITY_WINDOW = 2 * 60 * 60
logger = logging.getLogger(__name__)


def _connect_db():
    return connect_db(path=get_db_path())


def _ensure_status_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_status (
            name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            checked_at REAL,
            last_activity_at REAL,
            check_started_at REAL
        )
        """
    )


def _write_status(name, status, checked_at=None, last_activity_at=None):
    checked_at = time.time() if checked_at is None else checked_at
    conn = _connect_db()
    try:
        _ensure_status_table(conn)
        conn.execute(
            """
            INSERT INTO system_status
                (name, status, checked_at, last_activity_at, check_started_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(name) DO UPDATE SET
                status = excluded.status,
                checked_at = excluded.checked_at,
                last_activity_at = COALESCE(
                    excluded.last_activity_at, system_status.last_activity_at
                ),
                check_started_at = NULL
            """,
            (name, status, checked_at, last_activity_at),
        )
        conn.commit()
    finally:
        conn.close()


def _claim_check(name, minimum_interval):
    now = time.time()
    conn = _connect_db()
    try:
        _ensure_status_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT checked_at, check_started_at FROM system_status WHERE name = ?",
            (name,),
        ).fetchone()
        if row:
            checked_at, check_started_at = row
            if checked_at is not None and now - checked_at < minimum_interval:
                conn.commit()
                return False
            if check_started_at is not None and now - check_started_at < STATUS_PROBE_TIMEOUT * 2:
                conn.commit()
                return False
            conn.execute(
                "UPDATE system_status SET check_started_at = ? WHERE name = ?",
                (now, name),
            )
        else:
            conn.execute(
                """
                INSERT INTO system_status
                    (name, status, checked_at, last_activity_at, check_started_at)
                VALUES (?, 'unknown', NULL, NULL, ?)
                """,
                (name, now),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def record_notification_received(received_at=None):
    """Persist the last KNMI notification receipt as evidence of MQTT activity."""
    received_at = time.time() if received_at is None else received_at
    _write_status(
        "knmi_mqtt",
        "operational",
        checked_at=received_at,
        last_activity_at=received_at,
    )


def get_service_statuses():
    """Return persisted provider states, defaulting missing checks to unknown."""
    conn = _connect_db()
    try:
        _ensure_status_table(conn)
        rows = conn.execute(
            "SELECT name, status, checked_at, last_activity_at FROM system_status"
        ).fetchall()
    finally:
        conn.close()
    return {
        name: {
            "status": status,
            "checked_at": checked_at,
            "last_activity_at": last_activity_at,
        }
        for name, status, checked_at, last_activity_at in rows
    }


def _smtp_probe():
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not smtp_host:
        return "not_configured"

    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    use_ssl = os.getenv("SMTP_USE_SSL", "0").lower() in {"1", "true", "yes"}
    use_tls = os.getenv("SMTP_USE_TLS", "1").lower() in {"1", "true", "yes"}
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(smtp_host, smtp_port, timeout=STATUS_PROBE_TIMEOUT) as smtp:
        code, _ = smtp.ehlo()
        if code >= 400:
            raise smtplib.SMTPHeloError(code, b"SMTP EHLO failed")
        if use_tls and not use_ssl:
            smtp.starttls()
            code, _ = smtp.ehlo()
            if code >= 400:
                raise smtplib.SMTPHeloError(code, b"SMTP EHLO failed after STARTTLS")
        if smtp_username:
            smtp.login(smtp_username, smtp_password)
    return "operational"


def refresh_smtp_status():
    if not _claim_check("smtp", STATUS_CHECK_INTERVAL):
        return
    try:
        status = _smtp_probe()
    except (OSError, smtplib.SMTPException, ValueError, RuntimeError):
        logger.exception("SMTP connectivity check failed.")
        status = "outage"
    _write_status("smtp", status)


def _last_notification_received():
    conn = _connect_db()
    try:
        _ensure_status_table(conn)
        row = conn.execute(
            "SELECT last_activity_at FROM system_status WHERE name = 'knmi_mqtt'"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def _probe_mqtt():
    import paho.mqtt.client as mqtt_client
    import paho.mqtt.properties as mqtt_properties

    from metar import METAR_NOTIFICATION_TOPIC, NOTIFICATION_BROKER

    token = os.getenv("KNMI_NOTIFICATION_API_KEY", "").strip()
    if not token:
        return "not_configured"

    completed = Event()
    result = {"status": "outage"}
    client = mqtt_client.Client(
        callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
        client_id=f"skydive-theorie-status-{uuid.uuid4().hex}",
        protocol=mqtt_client.MQTTv5,
        transport="websockets",
    )
    client.tls_set(tls_version=ssl.PROTOCOL_TLS)
    client.username_pw_set("token", token)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", False):
            logger.warning("KNMI notification service rejected the status probe connection: %s", reason_code)
            completed.set()
            return
        rc, _ = client.subscribe(METAR_NOTIFICATION_TOPIC, qos=1)
        if rc != mqtt_client.MQTT_ERR_SUCCESS:
            logger.warning("Could not subscribe to KNMI topic %s during status check: %s", METAR_NOTIFICATION_TOPIC, rc)
            completed.set()

    def on_subscribe(client, userdata, mid, reason_codes, properties=None):
        if reason_codes and all(not getattr(code, "is_failure", False) for code in reason_codes):
            result["status"] = "operational"
        else:
            logger.warning("KNMI notification service rejected the status probe subscription: %s", reason_codes)
        completed.set()

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    connect_properties = mqtt_properties.Properties(mqtt_properties.PacketTypes.CONNECT)
    connect_properties.SessionExpiryInterval = 0
    client.connect_async(
        host=NOTIFICATION_BROKER,
        port=443,
        keepalive=60,
        clean_start=True,
        properties=connect_properties,
    )
    loop_started = False
    try:
        rc = client.loop_start()
        loop_started = True
        if rc != mqtt_client.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT loop failed with code {rc}")
        if not completed.wait(STATUS_PROBE_TIMEOUT):
            raise TimeoutError("MQTT connection or subscription timed out")
        return result["status"]
    finally:
        if loop_started:
            client.disconnect()
            client.loop_stop()


def refresh_mqtt_status():
    now = time.time()
    last_received = _last_notification_received()
    if last_received is not None and now - last_received < MQTT_ACTIVITY_WINDOW:
        _write_status(
            "knmi_mqtt",
            "operational",
            checked_at=now,
            last_activity_at=last_received,
        )
        return
    if not _claim_check("knmi_mqtt", STATUS_CHECK_INTERVAL):
        return
    try:
        status = _probe_mqtt()
    except (OSError, RuntimeError, ValueError, TimeoutError):
        logger.exception("KNMI MQTT connectivity check failed.")
        status = "outage"
    latest_received = _last_notification_received()
    if latest_received is not None and time.time() - latest_received < MQTT_ACTIVITY_WINDOW:
        status = "operational"
    _write_status("knmi_mqtt", status, last_activity_at=latest_received)


def _has_metar_for_today():
    today = datetime.now(timezone.utc).date().isoformat()
    conn = _connect_db()
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metar'"
        ).fetchone()
        if not table_exists:
            return False
        return conn.execute(
            "SELECT 1 FROM metar WHERE datetime = ? LIMIT 1",
            (today,),
        ).fetchone() is not None
    finally:
        conn.close()


def _probe_open_data():
    from metar import DEFAULT_OPEN_DATA_URL, METAR_DATASET, METAR_VERSION, _wait_for_api_slot

    api_key = os.getenv("KNMI_API_KEY", "").strip()
    if not api_key:
        return "not_configured"
    if not _wait_for_api_slot(wait=False):
        logger.warning("KNMI Open Data status probe skipped because the shared API quota is exhausted.")
        return "unknown"

    base_url = os.getenv("KNMI_OPEN_DATA_URL", DEFAULT_OPEN_DATA_URL).rstrip("/")
    endpoint = (
        f"{base_url}/v1/datasets/{quote(METAR_DATASET, safe='')}"
        f"/versions/{quote(METAR_VERSION, safe='')}/files?maxKeys=1"
    )
    request = Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "Authorization": api_key,
            "User-Agent": "skydive-theorie/1.0",
        },
    )
    with urlopen(request, timeout=STATUS_PROBE_TIMEOUT) as response:
        status = response.status if hasattr(response, "status") else response.getcode()
        if not 200 <= status < 300:
            raise URLError(f"KNMI Open Data returned HTTP {status}")
    return "operational"


def refresh_open_data_status():
    if _has_metar_for_today():
        _write_status("knmi_opendata", "operational")
        return
    if not _claim_check("knmi_opendata", OPENDATA_PROBE_INTERVAL):
        return
    try:
        status = _probe_open_data()
    except (HTTPError, URLError, OSError, ValueError, RuntimeError):
        logger.exception("KNMI Open Data connectivity check failed.")
        status = "outage"
    _write_status("knmi_opendata", status)


def refresh_system_status():
    """Refresh all provider statuses when their persisted check interval expires."""
    refresh_smtp_status()
    refresh_mqtt_status()
    refresh_open_data_status()


def system_status_monitor(stop_event=None):
    """Keep status checks fresh from the Gunicorn master process."""
    stop_event = stop_event or Event()
    while not stop_event.is_set():
        try:
            refresh_system_status()
        except (OSError, sqlite3.Error):
            logger.exception("Could not persist external service status.")
        if stop_event.wait(STATUS_MONITOR_INTERVAL):
            break
