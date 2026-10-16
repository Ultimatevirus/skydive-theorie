import os
from threading import Event, Thread

from metar import metar_refresh_loop
from taf import taf_refresh_loop


# Sized for a 4-core host; override per deployment (production compose runs several replicas).
workers = int(os.environ.get('GUNICORN_PROCESSES', '4'))

threads = int(os.environ.get('GUNICORN_THREADS', '6'))

timeout = int(os.environ.get('GUNICORN_TIMEOUT', '60'))

graceful_timeout = int(os.environ.get('GUNICORN_GRACEFUL_TIMEOUT', '30'))

keepalive = int(os.environ.get('GUNICORN_KEEPALIVE', '5'))

# Recycle workers periodically (staggered by jitter) to cap memory growth.
max_requests = int(os.environ.get('GUNICORN_MAX_REQUESTS', '2000'))

max_requests_jitter = int(os.environ.get('GUNICORN_MAX_REQUESTS_JITTER', '200'))

# Keep worker heartbeat files in RAM so slow disk I/O cannot make the master kill healthy workers.
if os.path.isdir('/dev/shm'):
	worker_tmp_dir = '/dev/shm'

bind = os.environ.get('GUNICORN_BIND', '0.0.0.0:5000')



forwarded_allow_ips = os.environ.get('FORWARDED_ALLOW_IPS', '127.0.0.1')

secure_scheme_headers = { 'X-Forwarded-Proto': 'https' }


_metar_refresh_stop = Event()
_metar_refresh_thread = None
_taf_refresh_thread = None


def when_ready(server):
	global _metar_refresh_thread, _taf_refresh_thread
	if os.environ.get('KNMI_METAR_REFRESH', '1').lower() not in {'0', 'false', 'no'}:
		_metar_refresh_thread = Thread(
			target=metar_refresh_loop,
			args=(_metar_refresh_stop,),
			name='metar-notifications',
			daemon=True,
		)
		_metar_refresh_thread.start()
	if os.environ.get('KNMI_TAF_REFRESH', '1').lower() not in {'0', 'false', 'no'}:
		_taf_refresh_thread = Thread(
			target=taf_refresh_loop,
			args=(_metar_refresh_stop,),
			name='taf-refresh',
			daemon=True,
		)
		_taf_refresh_thread.start()


def on_exit(server):
	_metar_refresh_stop.set()
	for thread in (_metar_refresh_thread, _taf_refresh_thread):
		if thread is not None:
			thread.join(timeout=5)