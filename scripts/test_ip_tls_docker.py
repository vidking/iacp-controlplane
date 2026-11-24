#!/usr/bin/env python3
"""Validar TLS por IP detrás de Docker NAT con una CA temporal y sin MNG."""

import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.prepare_pilot_ca import prepare
from api.service_tls import issue
from worker.service_recipes import render
from api.service_secrets import installation_credentials


def run(argv, **kwargs):
    return subprocess.run(
        argv, capture_output=True, text=True, check=True, **kwargs
    ).stdout.strip()


def validate():
    releases = json.loads((ROOT / "services/releases.json").read_text())
    with tempfile.TemporaryDirectory(prefix="iacp-ip-tls-") as folder:
        root = Path(folder)
        prepare(root, os.getuid(), os.getgid())
        os.environ.update(
            IACP_SERVICE_IP_TLS_ENABLED="1",
            IACP_SERVICE_CA_CERT_FILE=str(root / "pilot-ca.crt"),
            IACP_SERVICE_CA_KEY_FILE=str(root / "pilot-ca.key"),
        )
        for name, content in issue("127.0.0.1", allow_non_global=True).items():
            (root / name).write_text(content)
        context = ssl.create_default_context(cafile=str(root / "pilot-ca.crt"))
        for application in ["nextcloud", "n8n"]:
            credentials = installation_credentials()
            _, files = render(
                application,
                {"domain": "127.0.0.1", "access_mode": "ip", "admin_user": "admin"},
                credentials,
                releases[application],
            )
            config = (
                files["Caddyfile"]
                .replace("reverse_proxy app:80", 'respond "validado"')
                .replace("reverse_proxy app:5678", 'respond "validado"')
            )
            image = releases[application]["images"]["caddy"]
            (root / "Caddyfile").write_text(config)
            container = run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--rm",
                    "-p",
                    "127.0.0.1::443",
                    "-v",
                    folder + ":/etc/caddy:ro",
                    image,
                ]
            )
            try:
                port = json.loads(run(["docker", "inspect", container]))[0][
                    "NetworkSettings"
                ]["Ports"]["443/tcp"][0]["HostPort"]
                url = "https://127.0.0.1:" + port
                headers = {}
                for attempt in range(30):
                    try:
                        with urlopen(
                            Request(url, headers=headers), context=context, timeout=5
                        ) as response:
                            assert (
                                response.status == 200
                                and response.read() == b"validado"
                            )
                        break
                    except HTTPError:
                        raise
                    except OSError:
                        if attempt == 29:
                            raise
                        time.sleep(0.2)
                print(
                    json.dumps(
                        {
                            "application": application,
                            "tls_ip_verified": "PASS",
                            "proxy_auth": "none",
                        }
                    )
                )
            finally:
                run(["docker", "stop", container])


if __name__ == "__main__":
    validate()
