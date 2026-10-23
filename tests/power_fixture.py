"""Loopback-only HTTPS Proxmox fixture; no real credentials or infrastructure."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import subprocess
from threading import Event, Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit


@contextmanager
def power_server(folder, power="stopped", *, vmid=21001, name="fixture-kvm"):
    cert, key = folder / "power-cert.pem", folder / "power-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    store = SimpleNamespace(power=power, requests=[], task_running=False, task_seen=Event(), posts=0,
                            pending=[], exitstatus="OK", permission=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            path = unquote(urlsplit(self.path).path.removeprefix('/api2/json'))
            parameters = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
            store.requests.append((self.command, path, parameters))
            if self.headers.get('Authorization') != 'PVEAPIToken=synthetic-token':
                self.send_error(403)
                return
            if path == "/version":
                value = {"version": "fixture-pve"}
            elif path == "/access/permissions":
                value = {f"/vms/{vmid}": {"VM.Audit": 0, **({"VM.PowerMgmt": 0} if store.permission else {})}}
            elif path == f"/nodes/mng01/qemu/{vmid}/config":
                value = {"name": name, "agent": "0", "acpi": 1}
            elif path == f"/nodes/mng01/qemu/{vmid}/status/current":
                value = {"vmid": vmid, "status": store.power, "ha": {"managed": 0}}
            elif path == f"/nodes/mng01/qemu/{vmid}/pending":
                value = store.pending
            elif path.startswith("/nodes/mng01/tasks/"):
                store.task_seen.set()
                value = {"status": "running"} if store.task_running else {"status": "stopped", "exitstatus": store.exitstatus}
            elif self.command == "POST" and path.rsplit('/', 1)[-1] in {"start", "shutdown", "reboot"}:
                action = path.rsplit('/', 1)[-1]
                assert path == f"/nodes/mng01/qemu/{vmid}/status/" + action
                assert parameters == ({} if action == "start" else {"timeout": ["60"], **({"forceStop": ["0"]} if action == "shutdown" else {})})
                store.posts += 1
                store.power = "stopped" if action == "shutdown" else "running"
                typ = {"start": "qmstart", "shutdown": "qmshutdown", "reboot": "qmreboot"}[action]
                value = f"UPID:mng01:00000001:00000002:00000003:{typ}:{vmid}:synthetic@pve:"
            else:
                self.send_error(404)
                return
            payload = json.dumps({"data": value}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store.endpoint, store.ca_file = f'https://127.0.0.1:{server.server_port}', cert
    try:
        yield store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
