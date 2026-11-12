import subprocess
import unittest
from unittest.mock import MagicMock, patch

import gunicorn_config
import background_services


class BackgroundProcessTests(unittest.TestCase):
    def tearDown(self):
        gunicorn_config._background_process = None

    def test_background_services_run_in_a_separate_process_and_stop(self):
        process = MagicMock()
        process.poll.return_value = None
        with patch.dict("os.environ", {
            "SYSTEM_STATUS_REFRESH": "1",
            "KNMI_METAR_REFRESH": "0",
            "KNMI_TAF_REFRESH": "0",
        }), patch.object(gunicorn_config.subprocess, "Popen", return_value=process) as popen:
            gunicorn_config.when_ready(MagicMock())
            self.assertEqual(popen.call_args.args[0][1:], ["-m", "background_services"])
            gunicorn_config.on_exit(MagicMock())
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=20)

    def test_disabled_services_do_not_start_a_process(self):
        with patch.dict("os.environ", {
            "SYSTEM_STATUS_REFRESH": "0",
            "KNMI_METAR_REFRESH": "false",
            "KNMI_TAF_REFRESH": "no",
        }), patch.object(gunicorn_config.subprocess, "Popen") as popen:
            gunicorn_config.when_ready(MagicMock())
        popen.assert_not_called()

    def test_stuck_background_process_is_killed(self):
        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("background", 20), 0]
        gunicorn_config._background_process = process
        server = MagicMock()
        gunicorn_config.on_exit(server)
        process.kill.assert_called_once()
        server.log.warning.assert_called_once()

    def test_background_process_honors_flags_and_stops_threads(self):
        stop = MagicMock()
        thread = MagicMock()
        with patch.dict("os.environ", {
            "SYSTEM_STATUS_REFRESH": "1",
            "KNMI_METAR_REFRESH": "0",
            "KNMI_TAF_REFRESH": "0",
        }), patch.object(background_services, "Event", return_value=stop), \
             patch.object(background_services, "Thread", return_value=thread) as create_thread, \
             patch.object(background_services.signal, "signal"):
            background_services.main()
        create_thread.assert_called_once_with(
            target=background_services.system_status_monitor,
            args=(stop,),
            name="system-status-monitor",
            daemon=True,
        )
        thread.start.assert_called_once()
        stop.set.assert_called_once()
        thread.join.assert_called_once_with(timeout=5)
