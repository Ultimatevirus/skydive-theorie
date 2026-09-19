import os
from threading import Event, Thread

from metar import metar_refresh_loop



workers = int(os.environ.get('GUNICORN_PROCESSES', '2'))

threads = int(os.environ.get('GUNICORN_THREADS', '4'))

timeout = int(os.environ.get('GUNICORN_TIMEOUT', '120'))

bind = os.environ.get('GUNICORN_BIND', '0.0.0.0:5000')



forwarded_allow_ips = os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1')

secure_scheme_headers = { 'X-Forwarded-Proto': 'https' }


_metar_refresh_stop = Event()
_metar_refresh_thread = None


def when_ready(server):
	global _metar_refresh_thread
	if os.environ.get('KNMI_METAR_REFRESH', '1').lower() not in {'0', 'false', 'no'}:
		_metar_refresh_thread = Thread(
			target=metar_refresh_loop,
			args=(_metar_refresh_stop,),
			name='metar-refresh',
			daemon=True,
		)
		_metar_refresh_thread.start()


def on_exit(server):
	_metar_refresh_stop.set()
	if _metar_refresh_thread is not None:
		_metar_refresh_thread.join(timeout=5)