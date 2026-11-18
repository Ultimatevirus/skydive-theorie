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
HOST = "www.skydive-theorie.nl"


def docker(*args):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True).stdout


def request(port, path, host=HOST):
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
        # The transport file is the one shipped for production.
        shutil.copy(ROOT / "traefik/portainer.yml", Path(directory, "portainer.yml"))
        # Routing labels mirror the portainer service in compose.production.yml.
        labels = [
            "traefik.enable=true",
            f"traefik.docker.network={MARKER}",
            f"traefik.http.routers.portainer.rule=Host(`{HOST}`) && PathPrefix(`/portainer`)",
            "traefik.http.routers.portainer.entrypoints=websecure",
            "traefik.http.routers.portainer.tls=true",
            "traefik.http.routers.portainer.priority=10000",
            "traefik.http.routers.portainer.middlewares=portainer-stripprefix",
            "traefik.http.middlewares.portainer-stripprefix.stripprefix.prefixes=/portainer",
            "traefik.http.services.portainer.loadbalancer.server.port=9443",
            "traefik.http.services.portainer.loadbalancer.server.scheme=https",
            "traefik.http.services.portainer.loadbalancer.serversTransport=portainer-insecure@file",
        ]
        label_args = [arg for label in labels for arg in ("--label", label)]
        try:
            docker("network", "create", MARKER)
            docker(
                "run", "-d", "--name", PORTAINER, "--network", MARKER,
                "--network-alias", "portainer", *label_args,
                "--mount", "type=tmpfs,destination=/data",
                "portainer/portainer-ce:lts", "--base-url", "/portainer",
            )
            docker(
                "run", "-d", "--name", PROXY, "--network", MARKER,
                "-p", "127.0.0.1::443",
                "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
                "-v", f"{directory}/portainer.yml:/etc/traefik/portainer.yml:ro",
                "traefik:v3.7.18", "--entrypoints.websecure.address=:443",
                "--providers.docker=true", "--providers.docker.exposedbydefault=false",
                f"--providers.docker.network={MARKER}",
                "--providers.file.filename=/etc/traefik/portainer.yml",
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
                        "--host-resolver-rules=MAP www.skydive-theorie.nl 127.0.0.1",
                        f"--user-data-dir={directory}/chrome", "--virtual-time-budget=10000",
                        "--dump-dom", f"https://{HOST}:{port}/portainer/",
                    ],
                    text=True, capture_output=True, timeout=30, check=True,
                )
                assert 'id="base" href="/portainer/"' in browser.stdout, browser.stdout[:3000]

            assert request(port, "/portainer/", "other.skydive-theorie.nl")[0] == 404
            logs = subprocess.run(["docker", "logs", PROXY], text=True, capture_output=True)
            assert "servers transport not found" not in logs.stdout + logs.stderr
            print("Portainer path, assets, API and file-provider transport verified")
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
