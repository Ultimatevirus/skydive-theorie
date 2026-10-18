import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt_client
import system_status


class SystemStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "status.db")
        self.environment = patch.dict(os.environ, {"DB_PATH": self.db_path}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def test_smtp_checks_tls_and_authentication_without_sending_email(self):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        environment = {
            "SMTP_HOST": "smtp.example.test",
            "SMTP_PORT": "587",
            "SMTP_USERNAME": "mailer@example.test",
            "SMTP_PASSWORD": "secret",
            "SMTP_USE_TLS": "1",
        }

        with patch.dict(os.environ, environment, clear=False), patch.object(
            system_status.smtplib, "SMTP", return_value=smtp
        ) as smtp_factory:
            system_status.refresh_smtp_status()

        smtp_factory.assert_called_once_with(
            "smtp.example.test", 587, timeout=system_status.STATUS_PROBE_TIMEOUT
        )
        smtp.starttls.assert_called_once_with()
        smtp.login.assert_called_once_with("mailer@example.test", "secret")
        smtp.send_message.assert_not_called()
        self.assertEqual(system_status.get_service_statuses()["smtp"]["status"], "operational")

    def test_smtp_without_host_is_reported_as_not_configured(self):
        with patch.dict(os.environ, {"SMTP_HOST": ""}, clear=False):
            system_status.refresh_smtp_status()

        self.assertEqual(system_status.get_service_statuses()["smtp"]["status"], "not_configured")

    def test_recent_mqtt_notification_skips_broker_probe(self):
        system_status.record_notification_received()

        with patch.object(system_status, "_probe_mqtt") as mqtt_probe:
            system_status.refresh_mqtt_status()

        mqtt_probe.assert_not_called()
        status = system_status.get_service_statuses()["knmi_mqtt"]
        self.assertEqual(status["status"], "operational")
        self.assertLess(time.time() - status["last_activity_at"], 5)

    def test_mqtt_broker_is_probed_when_no_recent_notification_exists(self):
        with patch.object(system_status, "_probe_mqtt", return_value="operational") as mqtt_probe:
            system_status.refresh_mqtt_status()

        mqtt_probe.assert_called_once_with()
        self.assertEqual(system_status.get_service_statuses()["knmi_mqtt"]["status"], "operational")

    def test_mqtt_probe_connects_and_waits_for_subscription_acknowledgement(self):
        client = MagicMock()

        class AcceptedReason:
            is_failure = False

        def start_loop():
            client.on_connect(client, None, None, AcceptedReason(), None)
            client.on_subscribe(client, None, 1, [AcceptedReason()], None)
            return mqtt_client.MQTT_ERR_SUCCESS

        client.loop_start.side_effect = start_loop
        with patch.dict(os.environ, {"KNMI_NOTIFICATION_API_KEY": "notification-key"}), patch(
            "paho.mqtt.client.Client", return_value=client
        ):
            status = system_status._probe_mqtt()

        self.assertEqual(status, "operational")
        client.connect_async.assert_called_once()
        client.subscribe.assert_called_once()
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()

    def test_today_metar_row_suppresses_open_data_api_request(self):
        today = datetime.now(timezone.utc).date().isoformat()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE metar (datetime TEXT, airport TEXT, metar TEXT)")
            conn.execute("INSERT INTO metar VALUES (?, ?, ?)", (today, "EHAM", "METAR"))
            conn.commit()
        finally:
            conn.close()

        with patch.object(system_status, "urlopen") as open_data_request:
            system_status.refresh_open_data_status()

        open_data_request.assert_not_called()
        self.assertEqual(system_status.get_service_statuses()["knmi_opendata"]["status"], "operational")

    def test_missing_today_metar_probes_open_data_with_single_item_list_request(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        environment = {
            "KNMI_API_KEY": "test-api-key",
            "KNMI_OPEN_DATA_URL": "https://knmi.example.test/open-data",
        }

        with patch.dict(os.environ, environment, clear=False), patch.object(
            system_status, "urlopen", return_value=response
        ) as open_data_request, patch(
            "metar._wait_for_api_slot", return_value=True
        ) as api_quota:
            system_status.refresh_open_data_status()

        request = open_data_request.call_args.args[0]
        self.assertIn("/v1/datasets/metar/versions/1.0/files?maxKeys=1", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "test-api-key")
        api_quota.assert_called_once_with(wait=False)
        open_data_request.assert_called_once_with(
            request, timeout=system_status.STATUS_PROBE_TIMEOUT
        )
        self.assertEqual(system_status.get_service_statuses()["knmi_opendata"]["status"], "operational")

    def test_open_data_api_probe_is_rate_limited_when_today_has_no_metar(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        environment = {"KNMI_API_KEY": "test-api-key"}

        with patch.dict(os.environ, environment, clear=False), patch.object(
            system_status, "urlopen", return_value=response
        ) as open_data_request:
            system_status.refresh_open_data_status()
            system_status.refresh_open_data_status()

        open_data_request.assert_called_once()
