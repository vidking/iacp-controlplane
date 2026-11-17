#!/usr/bin/env python3
"""Prueba local aislada de Nextcloud, PostgreSQL, Redis, Caddy, TLS y persistencia."""

import base64
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener, urlopen

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run(argv, *, check=True, timeout=60):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("Falló una operación Docker local; se conserva solo su etapa, sin secretos.")
    return result


def main():
    from scripts.prepare_pilot_ca import prepare
    from worker.service_recipes import render
    from worker.service_tls import issue

    releases = json.loads((ROOT / "services/releases.json").read_text())
    release = releases["nextcloud"]
    project = "iacp-nextcloud-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="iacp-nextcloud-docker-") as temp:
        folder = Path(temp)
        prepare(folder, os.getuid(), os.getgid())
        os.environ.update(IACP_SERVICE_IP_TLS_ENABLED="1", IACP_SERVICE_CA_DIR=temp)
        for name, content in issue("127.0.0.1").items():
            (folder / name).write_text(content)

        password = secrets.token_urlsafe(32) + "aA9!"
        credentials = {
            "database_password": secrets.token_urlsafe(36),
            "admin_password": password,
            "encryption_key": secrets.token_urlsafe(36),
            "gateway_token": secrets.token_urlsafe(36),
        }
        compose, files = render(
            "nextcloud",
            {"domain": "127.0.0.1", "access_mode": "ip", "admin_user": "admin"},
            credentials,
            release,
        )
        compose["services"]["caddy"]["ports"] = ["127.0.0.1::443"]
        for name, content in files.items():
            (folder / name).write_text(content)
        compose_path = folder / "compose.yaml"
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False))
        command = ["docker", "compose", "--project-name", project, "-f", str(compose_path)]
        try:
            run(command + ["up", "-d", "--wait", "--wait-timeout", "600"], timeout=660)
            port = run(command + ["port", "caddy", "443"]).stdout.strip().rsplit(":", 1)[-1]
            url = f"https://127.0.0.1:{port}"
            context = ssl.create_default_context(cafile=str(folder / "pilot-ca.crt"))
            for _ in range(600):
                try:
                    req = Request(url + "/status.php", headers={"Host": "127.0.0.1"})
                    with urlopen(req, context=context, timeout=5) as response:
                        details = json.loads(response.read())
                    if response.status == 200 and details.get("installed") is True:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Nextcloud no alcanzó readiness HTTPS tras completar la instalación local.")

            status = run(command + ["exec", "-T", "--user", "www-data", "app", "php", "occ", "status", "--output=json"])
            assert json.loads(status.stdout).get("installed") is True, "OCC_INSTALL_STATUS_FAILED"
            trusted = run(command + ["exec", "-T", "--user", "www-data", "app", "php", "occ", "config:system:get", "trusted_domains", "1"])
            assert trusted.stdout.strip() == "127.0.0.1", "NEXTCLOUD_IP_NOT_TRUSTED"

            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, request, file, code, message, headers, new_url):
                    return None

            opener = build_opener(HTTPSHandler(context=context), NoRedirect())
            current = url + "/"
            for _ in range(5):
                request = Request(current, headers={"Host": "127.0.0.1"})
                try:
                    with opener.open(request, timeout=15) as home:
                        page_status, page_body = home.status, home.read()
                        location = home.headers.get("Location")
                except HTTPError as response:
                    page_status, page_body = response.code, response.read()
                    location = response.headers.get("Location")
                if not 300 <= page_status < 400 or not location:
                    break
                target = urlsplit(location)
                if target.hostname == "127.0.0.1" and target.port is None:
                    target = target._replace(netloc=f"127.0.0.1:{port}")
                current = urlunsplit(target)
            homepage = page_body.decode(errors="replace").lower()
            assert page_status == 200, "NEXTCLOUD_LOGIN_PAGE_UNAVAILABLE"
            assert "untrusted domain" not in homepage and "dominio del que no se confía" not in homepage, "NEXTCLOUD_UNTRUSTED_HOSTNAME"
            token = base64.b64encode(("admin:" + password).encode()).decode()
            path = "/remote.php/dav/files/admin/iacp-validation.txt"

            def request(method, data=None):
                req = Request(url + path, data=data, method=method, headers={
                    "Authorization": "Basic " + token,
                    "Host": "127.0.0.1",
                })
                with urlopen(req, context=context, timeout=30) as response:
                    return response.status, response.read()

            for _ in range(180):
                try:
                    req = Request(url + "/status.php", headers={"Host": "127.0.0.1"})
                    with urlopen(req, context=context, timeout=5) as response:
                        health = json.loads(response.read())
                    if response.status == 200 and health.get("installed") is True:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Nextcloud no respondió readiness HTTPS detrás de Caddy.")

            payload = b"nextcloud-persistent-validation"
            written, _ = request("PUT", payload)
            assert written in {201, 204}, "WEBDAV_WRITE_FAILED"
            assert request("GET")[1] == payload, "WEBDAV_READ_FAILED"
            run(command + ["restart", "app"])
            for _ in range(300):
                try:
                    req = Request(url + "/status.php", headers={"Host": "127.0.0.1"})
                    with urlopen(req, context=context, timeout=5) as response:
                        details = json.loads(response.read())
                    if response.status == 200 and details.get("installed") is True:
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Nextcloud no recuperó readiness HTTPS después de reiniciar.")
            status = run(command + ["exec", "-T", "--user", "www-data", "app", "php", "occ", "status", "--output=json"])
            assert json.loads(status.stdout).get("installed") is True, "OCC_RESTART_STATUS_FAILED"
            assert request("GET")[1] == payload, "DATA_NOT_PERSISTED_AFTER_RESTART"
            print(json.dumps({
                "result": "PASS",
                "scope": "Docker local con imágenes fijadas; no certificación de SO ni de MNG",
                "nextcloud_version": release["version"],
                "database": "PostgreSQL",
                "https_ip_ca": "PASS",
                "webdav_write_read": "PASS",
                "occ_installed": "PASS",
                "persistence_after_app_restart": "PASS",
            }))
        finally:
            run(command + ["down", "--remove-orphans", "--timeout", "20"], check=False)
            listed = run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], check=False)
            volumes = listed.stdout.split()
            if volumes:
                run(["docker", "volume", "rm", *volumes], check=False)


if __name__ == "__main__":
    main()
