"""Run per-container background services outside the Gunicorn master."""

import os
import signal
from threading import Event, Thread

from metar import metar_refresh_loop
from system_status import system_status_monitor
from taf import taf_refresh_loop


def main():
    stop = Event()
    threads = []

    def terminate(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    services = (
        ("SYSTEM_STATUS_REFRESH", "system-status-monitor", system_status_monitor),
        ("KNMI_METAR_REFRESH", "metar-notifications", metar_refresh_loop),
        ("KNMI_TAF_REFRESH", "taf-refresh", taf_refresh_loop),
    )
    try:
        for flag, name, target in services:
            if os.environ.get(flag, "1").lower() not in {"0", "false", "no"}:
                thread = Thread(target=target, args=(stop,), name=name, daemon=True)
                thread.start()
                threads.append(thread)
        stop.wait()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
