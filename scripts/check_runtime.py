"""Comprueba el flujo HTTP completo desde un contenedor, sin dependencias del host."""
import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(base_url, path, token=None, method="GET"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urlopen(Request(base_url + path, headers=headers, method=method), timeout=10) as response:
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
    status, blocked = request(base_url, "/v1/instances", token, "POST")
    assert status == 503 and blocked["code"] == "IAC_EXECUTOR_NOT_CONFIGURED"
    status, contract = request(base_url, "/openapi.json")
    assert status == 200 and "/v1/jobs/smoke" in contract["paths"]
    return {"status": "passed", "job_id": job_id, "job_status": result["status"], "provisioning": "disabled"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    print(json.dumps(check(args.base_url.rstrip("/"), args.timeout)))
