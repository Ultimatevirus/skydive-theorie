import os
import subprocess
import sys


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


_background_process = None


def when_ready(server):
	global _background_process
	if any(os.environ.get(flag, '1').lower() not in {'0', 'false', 'no'} for flag in (
		'SYSTEM_STATUS_REFRESH', 'KNMI_METAR_REFRESH', 'KNMI_TAF_REFRESH',
	)):
		_background_process = subprocess.Popen([sys.executable, '-m', 'background_services'])


def on_exit(server):
	if _background_process is not None and _background_process.poll() is None:
		_background_process.terminate()
		try:
			_background_process.wait(timeout=20)
		except subprocess.TimeoutExpired:
			server.log.warning('Background services did not stop; killing process')
			_background_process.kill()
			_background_process.wait()