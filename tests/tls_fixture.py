"""Material efímero de pruebas, creado al ejecutar Docker; nunca autoriza un entorno real."""
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ROOT = Path("/test-tls")
spec = importlib.util.spec_from_file_location("tls_renderer", Path(__file__).resolve().parents[1] / "scripts/render-tls.py")
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
POLICY = {"lan_cidrs": ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
          "crm_source_cidrs": ["198.51.100.10/32"], "crm_origins": ["https://crm.fixture.invalid"]}


def render(policy):
    temporary = ROOT / "iacp.tmp"
    temporary.write_text(json.dumps(renderer.build(policy)))
    temporary.replace(ROOT / "iacp.yml")


def init():
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=iac.cyberfuel.com", "-addext", "subjectAltName=DNS:iac.cyberfuel.com",
                    "-keyout", str(ROOT / "privkey.pem"), "-out", str(ROOT / "fullchain.pem")],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (ROOT / "privkey.pem").chmod(0o600)
    render(POLICY)


def api():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401 if self.path.startswith("/v1") else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"fixture": true}')
        def log_message(self, *args):
            pass
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()


def client():
    context = ssl.create_default_context(cafile=str(ROOT / "fullchain.pem"))
    with httpx.Client(verify=context, trust_env=False, timeout=2) as client:
        deadline = time.monotonic() + 25
        while True:
            try:
                response = client.get("https://iac.cyberfuel.com/health/live")
                if response.status_code == 200: break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline: raise AssertionError("HTTPS no quedó disponible")
            time.sleep(.2)
        assert client.get("https://iac.cyberfuel.com/v1/instances").status_code == 401
        assert client.get("https://iac.cyberfuel.com/health/live", headers={"Origin": POLICY["crm_origins"][0]}).status_code == 200
        assert client.get("https://iac.cyberfuel.com/health/live", headers={"Origin": "https://unapproved.invalid"}).status_code == 404
        assert client.get("http://traefik:8080/health/live").status_code == 200
        try:
            httpx.get("https://iac.cyberfuel.com/health/live", trust_env=False, timeout=2)
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("El certificado de pruebas no debe ser confiable por defecto")
        render({**POLICY, "lan_cidrs": ["192.0.2.0/24"]})
        deadline = time.monotonic() + 12
        while client.get("https://iac.cyberfuel.com/health/live", headers={"X-Forwarded-For": "198.51.100.10"}).status_code != 403:
            if time.monotonic() > deadline: raise AssertionError("IP no autorizada o XFF falsificado aceptados")
            time.sleep(.2)
        assert client.get("http://traefik:8080/health/live").status_code == 200
    print("HTTPS y CA verificados; 401 conservado; orígenes y XFF/IP rechazados; diagnóstico local disponible.")


if __name__ == "__main__":
    {"init": init, "api": api, "client": client}[sys.argv[1]]()
