"""Material efímero de pruebas, creado al ejecutar Docker; nunca autoriza un entorno real."""
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import sys
import threading
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
                    "-subj", "/CN=iac.cyberfuel.com", "-addext", "subjectAltName=DNS:iac.cyberfuel.com,DNS:identity.fixture.invalid",
                    "-keyout", str(ROOT / "privkey.pem"), "-out", str(ROOT / "fullchain.pem")],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (ROOT / "privkey.pem").chmod(0o600)
    render(POLICY)


def api():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            protected = self.path.startswith(("/v1", "/docs", "/openapi.json"))
            self.send_response(401 if protected and self.headers.get("Authorization") != "Bearer fixture-token" else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({'fixture':True,'transport':self.headers.get('X-IACP-Transport'),
                'forwarded_proto':self.headers.get('X-Forwarded-Proto')}).encode())
        def log_message(self, *args):
            pass
    class Gateway(BaseHTTPRequestHandler):
        def do_GET(self):
            # The API bearer must never be forwarded to the corporate gateway.
            self.send_response(500 if self.headers.get("Authorization") else
                               200 if self.headers.get("Cookie") == "fixture-session=approved" else 401)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = HTTPServer(("0.0.0.0", 8443), Gateway)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT / "fullchain.pem", ROOT / "privkey.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
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
        public = renderer.build_public({**POLICY, "admin_forward_auth_url": "https://identity.fixture.invalid:8443/verify"})
        public["http"]["middlewares"]["corporate-identity"]["forwardAuth"]["tls"]["ca"] = "/run/tls/fullchain.pem"
        temporary = ROOT / "iacp.tmp"
        temporary.write_text(json.dumps(public))
        temporary.replace(ROOT / "iacp.yml")
        deadline = time.monotonic() + 12
        while True:
            redirect = client.get("http://traefik/docs", headers={"Host": "iac.cyberfuel.com"})
            if redirect.status_code in {301, 308} and redirect.headers.get("Location") == "https://iac.cyberfuel.com/docs":
                break
            if time.monotonic() > deadline:
                raise AssertionError(f"Public router did not reload: {redirect.status_code}, {redirect.headers.get('Location')}")
            time.sleep(.2)
        assert client.get("https://iac.cyberfuel.com/docs", headers={"Authorization": "Bearer fixture-token"}).status_code == 401
        assert client.get("https://iac.cyberfuel.com/docs", headers={"Cookie": "fixture-session=approved"}).status_code == 401
        identity = {"Cookie": "fixture-session=approved", "Authorization": "Bearer fixture-token"}
        assert client.get("https://iac.cyberfuel.com/docs", headers=identity).status_code == 200
        assert client.get("https://iac.cyberfuel.com/openapi.json", headers=identity).status_code == 200
        attested=client.get('https://iac.cyberfuel.com/health/live',headers={**identity,'X-IACP-Transport':'http'}).json()
        assert attested['transport']=='https' and attested['forwarded_proto'] is None
        diagnostic=client.get('http://traefik:8080/health/live',headers={'X-IACP-Transport':'https'}).json()
        assert diagnostic['transport'] is None
        assert client.get("https://iac.cyberfuel.com/docs", headers={"X-Forwarded-Prefix": "/verify", "Authorization": "Bearer fixture-token"}).status_code == 401
        assert client.post("https://iac.cyberfuel.com/v1/instances", headers=identity, content=b"x" * 65537).status_code == 413
        statuses = [client.get("https://iac.cyberfuel.com/docs").status_code for _ in range(30)]
        assert 429 in statuses
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
        for mode in ("gateway", "approved-internal-policy"):
            internal_policy = {**POLICY, "crm_source_cidrs": ["10.255.255.254/32"], "identity_mode": mode}
            if mode == "gateway":
                internal_policy["admin_forward_auth_url"] = "https://identity.fixture.invalid:8443/verify"
            configuration = renderer.build_public(internal_policy, internal=True)
            if mode == "gateway":
                configuration["http"]["middlewares"]["corporate-identity"]["forwardAuth"]["tls"]["ca"] = "/run/tls/fullchain.pem"
            temporary = ROOT / "iacp.tmp"
            temporary.write_text(json.dumps(configuration))
            temporary.replace(ROOT / "iacp.yml")
            deadline = time.monotonic() + 12
            while client.get("https://iac.cyberfuel.com/docs", headers=identity).status_code != 200:
                if time.monotonic() > deadline: raise AssertionError("Internal router failed to reload")
                time.sleep(.2)
            assert client.get("https://iac.cyberfuel.com/docs").status_code == 401
            assert client.get("https://iac.cyberfuel.com/openapi.json").status_code == 401
            assert client.get("https://iac.cyberfuel.com/openapi.json", headers=identity).status_code == 200
            if mode == "gateway":
                assert client.get("https://iac.cyberfuel.com/docs", headers={"Authorization": "Bearer fixture-token"}).status_code == 401
            else:
                assert client.get("https://iac.cyberfuel.com/docs", headers={"Authorization": "Bearer fixture-token"}).status_code == 200
            configuration["http"]["middlewares"]["internal-sources"]["ipAllowList"]["sourceRange"] = ["10.22.0.0/16"]
            temporary.write_text(json.dumps(configuration))
            temporary.replace(ROOT / "iacp.yml")
            deadline = time.monotonic() + 12
            while client.get("https://iac.cyberfuel.com/docs", headers={**identity, "X-Forwarded-For": "10.22.0.1"}).status_code != 403:
                if time.monotonic() > deadline: raise AssertionError("Internal unapproved source accepted")
                time.sleep(.2)
    print("TLS fixture passed: public and corporate-internal (gateway/internal policy), auth, source limits, redirect, headers and request limits. No production/WAN claim.")


if __name__ == "__main__":
    {"init": init, "api": api, "client": client}[sys.argv[1]]()
