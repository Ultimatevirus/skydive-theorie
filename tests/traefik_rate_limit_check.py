import http.client
import json
import os
import ssl
import subprocess
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MARKER = f"skydive-rate-test-{uuid.uuid4().hex[:12]}"
CONTAINERS = [f"{MARKER}-app", f"{MARKER}-static", f"{MARKER}-proxy"]


def run(*arguments, **kwargs):
    return subprocess.run(arguments, check=True, text=True, **kwargs)


def docker(*arguments, **kwargs):
    return run("docker", *arguments, **kwargs)


def get_labels():
    environment = os.environ.copy()
    environment.update({
        "SECRET_KEY": "integration-test-only",
        "TRAEFIK_HOST": "www.example.test",
        "TRAEFIK_APEX_HOST": "example.test",
        "ACME_EMAIL": "test@example.test",
        "APP_RATE_LIMIT_AVERAGE": "60",
        "APP_RATE_LIMIT_BURST": "3",
        "STATIC_RATE_LIMIT_AVERAGE": "60",
        "STATIC_RATE_LIMIT_BURST": "3",
        "APEX_RATE_LIMIT_AVERAGE": "60",
        "APEX_RATE_LIMIT_BURST": "3",
    })
    result = run(
        "docker", "compose", "-f", str(ROOT / "compose.production.yml"),
        "config", "--format", "json", capture_output=True, env=environment,
    )
    return json.loads(result.stdout)["services"]


def start_backend(name, labels):
    labels = {
        key: value.replace("$$", "$") for key, value in labels.items()
        if ".healthcheck." not in key and not key.endswith(".tls.certresolver")
    }
    labels["traefik.docker.network"] = MARKER
    labels["test.scope"] = MARKER
    for key in labels:
        if key.endswith(".loadbalancer.server.port"):
            labels[key] = "8000"
    arguments = ["run", "-d", "--name", name, "--network", MARKER]
    for key, value in labels.items():
        arguments.extend(["--label", f"{key}={value}"])
    docker(*arguments, "python:3.15-alpine3.24", "python", "-m", "http.server", "8000", capture_output=True)


def request(port, host, path="/", forwarded=None):
    connection = http.client.HTTPSConnection(
        "127.0.0.1", port, timeout=3, context=ssl._create_unverified_context()
    )
    headers = {"Host": host}
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        response.read()
        return response.status, dict(response.getheaders())
    finally:
        connection.close()


def main():
    services = get_labels()
    try:
        docker("network", "create", MARKER, capture_output=True)
        start_backend(CONTAINERS[0], services["app"]["labels"])
        start_backend(CONTAINERS[1], services["static"]["labels"])
        docker(
            "run", "-d", "--name", CONTAINERS[2], "--network", MARKER,
            "-p", "127.0.0.1::443", "-v", "/var/run/docker.sock:/var/run/docker.sock:ro",
            "traefik:v3.7.18", "--entrypoints.websecure.address=:443",
            "--providers.docker=true", "--providers.docker.exposedbydefault=false",
            f"--providers.docker.network={MARKER}",
            f"--providers.docker.constraints=Label(`test.scope`, `{MARKER}`)",
            capture_output=True,
        )
        port = int(docker("port", CONTAINERS[2], "443/tcp", capture_output=True).stdout.strip().rsplit(":", 1)[1])
        deadline = time.monotonic() + 15
        while True:
            try:
                status, _ = request(port, "www.example.test")
                if status == 200:
                    break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Traefik did not discover the test router")
            time.sleep(0.1)

        time.sleep(3.1)
        for _ in range(3):
            status, _ = request(port, "www.example.test")
            assert status == 200, status
        assert request(port, "www.example.test", forwarded="198.51.100.1")[0] == 429
        assert request(port, "www.example.test", forwarded="198.51.100.2")[0] == 429

        client_code = (
            "import http.client,ssl; "
            f"connection=http.client.HTTPSConnection('{CONTAINERS[2]}',443,"
            "context=ssl._create_unverified_context()); "
            "connection.request('GET','/',headers={'Host':'www.example.test'}); "
            "print(connection.getresponse().status)"
        )
        for container in CONTAINERS[:2]:
            status = docker("exec", container, "python", "-c", client_code, capture_output=True).stdout.strip()
            assert status == "200", status

        for _ in range(3):
            assert request(port, "www.example.test", "/static/missing")[0] == 404
        assert request(port, "www.example.test", "/static/missing")[0] == 429

        for _ in range(3):
            status, headers = request(port, "example.test", "/test")
            assert status in (301, 302, 307, 308)
            assert headers["Location"] == "https://www.example.test/test", headers["Location"]
        assert request(port, "example.test", "/test")[0] == 429

        time.sleep(1.1)
        assert request(port, "www.example.test")[0] == 200
        print("Traefik app, static and apex limits, distinct clients, spoofed headers, redirect and refill verified")
    finally:
        subprocess.run(["docker", "rm", "-f", *CONTAINERS], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "network", "rm", MARKER], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()