"""Comprueba el flujo HTTP completo desde un contenedor, sin dependencias del host."""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


def request(base_url, path, token=None, method="GET", body=None, extra_headers=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    headers.update(extra_headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(base_url + path, headers=headers, method=method, data=data), timeout=10) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def check(base_url: str, timeout: float = 60) -> dict:
    token = Path(os.environ["IACP_API_TOKEN_FILE"]).read_text().strip()
    deadline = time.monotonic() + timeout
    while True:
        try:
            if request(base_url, "/health/ready") == (200, {"status": "ready"}):
                break
        except (OSError, URLError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("El runtime no alcanzó readiness")
        time.sleep(0.2)
    assert request(base_url, "/health/live") == (200, {"status": "live"})
    assert request(base_url, "/v1/jobs/smoke", method="POST")[0] == 401
    status, accepted = request(base_url, "/v1/jobs/smoke", token, "POST")
    assert status == 202, "No se aceptó el job smoke"
    job_id = accepted["job_id"]
    deadline = time.monotonic() + timeout
    while True:
        status, result = request(base_url, f"/v1/jobs/{job_id}", token)
        assert status == 200
        if result["status"] == "completed":
            break
        if result["status"] == "failed" or time.monotonic() >= deadline:
            raise RuntimeError("El worker no completó el job smoke")
        time.sleep(0.2)
    # La admisión comercial vigente requiere un cuerpo completo y HTTPS antes
    # de crear una intención. El smoke interno usa HTTP deliberadamente, por
    # lo que debe comprobar el rechazo seguro y no una respuesta simulada.
    key = uuid4().hex
    spec = {
        "name": "check-" + key[:12],
        "plan_id": "kvm-1",
        "image_id": "ubuntu-2404",
        "network": {"profile_id": "lab-dhcp"},
        "access": {"root_password": "dev-check-" + key + "-password", "ssh_public_keys": []},
        "start_on_create": False,
    }
    headers = {"Idempotency-Key": key}
    rejected = (422, {"detail": "ROOT_ACCESS_REQUIRES_HTTPS"})
    assert request(base_url, "/v1/instances", token, "POST", spec, headers) == rejected
    assert request(base_url, "/v1/instances", token, "POST", spec, headers) == rejected
    status, contract = request(base_url, "/openapi.json", token)
    assert status == 200 and "/v1/jobs/smoke" in contract["paths"]
    return {"status": "passed", "job_id": job_id, "job_status": result["status"],
            "operation_status": "not-admitted", "provisioning": "https-required"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    print(json.dumps(check(args.base_url.rstrip("/"), args.timeout)))
