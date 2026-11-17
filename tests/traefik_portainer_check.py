"""Exercise the production Portainer router against isolated Traefik and Portainer containers."""

import http.client
import json
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MARKER = f"skydive-portainer-test-{uuid.uuid4().hex[:12]}"
PORTAINER = f"{MARKER}-portainer"
PROXY = f"{MARKER}-proxy"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True).stdout


def request(port, path, host="skydive-theorie.nl"):
    connection = http.client.HTTPSConnection(
        "127.0.0.1", port, timeout=5, context=ssl._create_unverified_context()
    )
    try:
        connection.request("GET", path, headers={"Host": host})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def main():
    with tempfile.TemporaryDirectory() as directory:
        config = (ROOT / "traefik/portainer.yml").read_text()
        # The isolated test has no ACME resolver or public DNS.
        config = config.replace("      tls:\n        certResolver: letsencrypt\n", "      tls: {}\n")
        Path(directory, "portainer.yml").write_text(config)
        Path(directory, "site.yml").write_text(
            """http:
  routers:
    apex:
      rule: Host(`skydive-theorie.nl`)
      entryPoints: [websecure]
      tls: {}
      service: noop@internal
      middlewares: [www-redirect]
    website:
      rule: Host(`www.skydive-theorie.nl`)
      entryPoints: [websecure]
      tls: {}
      service: noop@internal
  middlewares:
    www-redirect:
      redirectRegex:
        regex: '^https?://skydive-theorie\\.nl/(.*)'
        replacement: 'https://www.skydive-theorie.nl/${1}'
        permanent: true
"""
        )
        try:
            docker("network", "create", MARKER)
            docker(
                "run", "-d", "--name", PORTAINER, "--network", MARKER,
                "--network-alias", "host.docker.internal",
                "--mount", "type=tmpfs,destination=/data",
                "portainer/portainer-ce:lts", "--base-url", "/portainer/",
            )
            docker(
                "run", "-d", "--name", PROXY, "--network", MARKER,
                "-p", "127.0.0.1::443",
                "-v", f"{directory}:/etc/traefik/dynamic:ro",
                "traefik:v3.7.18", "--entrypoints.websecure.address=:443",
                "--providers.file.directory=/etc/traefik/dynamic",
            )
            port = int(docker("port", PROXY, "443/tcp").strip().rsplit(":", 1)[1])
            deadline = time.monotonic() + 30
            status = None
            while True:
                try:
                    status, headers, body = request(port, "/portainer/")
                    if status == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    proxy_logs = subprocess.run(
                        ["docker", "logs", PROXY], text=True, capture_output=True
                    )
                    portainer_logs = subprocess.run(
                        ["docker", "logs", PORTAINER], text=True, capture_output=True
                    )
                    raise RuntimeError(
                        f"Portainer did not become available: {status}\n"
                        f"Traefik: {proxy_logs.stdout}{proxy_logs.stderr}\n"
                        f"Portainer: {portainer_logs.stdout}{portainer_logs.stderr}"
                    )
                time.sleep(0.2)

            status, headers, _ = request(port, "/portainer?from=test")
            assert status in (301, 308), status
            assert headers["Location"] == "https://skydive-theorie.nl/portainer/?from=test", headers

            assert b'<base id="base"' in body, body[:1000]
            asset = re.search(rb'<script[^>]+src="([^"/][^"]*\.js)"', body)
            assert asset, body[:2000]
            asset_path = asset.group(1).decode().lstrip("/")
            assert request(port, f"/portainer/{asset_path}")[0] == 200
            status, _, body = request(port, "/portainer/api/status")
            assert status == 200 and "Version" in json.loads(body), (status, body)

            chrome = shutil.which("google-chrome")
            if chrome:
                browser = subprocess.run(
                    [
                        chrome, "--headless", "--no-sandbox", "--disable-gpu",
                        "--no-proxy-server", "--ignore-certificate-errors",
                        "--host-resolver-rules=MAP skydive-theorie.nl 127.0.0.1",
                        f"--user-data-dir={directory}/chrome", "--virtual-time-budget=10000",
                        "--dump-dom", f"https://skydive-theorie.nl:{port}/portainer/",
                    ],
                    text=True, capture_output=True, timeout=30, check=True,
                )
                assert 'id="base" href="/portainer/"' in browser.stdout, browser.stdout[:3000]

            for path in ("/", "/portainer-other"):
                status, headers, _ = request(port, path)
                assert status in (301, 308), status
                assert headers["Location"] == f"https://www.skydive-theorie.nl{path}", headers
            assert request(port, "/portainer/", "admin.skydive-theorie.nl")[0] == 404
            print("Portainer path, assets, API, apex redirect and former host verified")
        finally:
            subprocess.run(
                ["docker", "rm", "-f", PROXY, PORTAINER],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["docker", "network", "rm", MARKER],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    main()
