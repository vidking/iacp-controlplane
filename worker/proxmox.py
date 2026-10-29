import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPSHandler


class ProxmoxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def credentials() -> tuple[str, str]:
    path = os.getenv("IACP_PROXMOX_CREDENTIALS_FILE", "/run/secrets/proxmox_credentials")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise ProxmoxError(f"no se pudo leer credencial Proxmox: {error}") from error
    endpoint = data.get("PROXMOX_VE_ENDPOINT") or data.get("endpoint")
    token = data.get("PROXMOX_VE_API_TOKEN") or data.get("api_token")
    if not endpoint or not token:
        raise ProxmoxError("credencial Proxmox incompleta")
    return str(endpoint).rstrip("/"), str(token)


def load_environment() -> tuple[str, str]:
    endpoint, token = credentials()
    os.environ["PROXMOX_VE_ENDPOINT"] = endpoint
    os.environ["PROXMOX_VE_API_TOKEN"] = token
    return endpoint, token


class ProxmoxClient:
    def __init__(self, endpoint: str | None = None, token: str | None = None):
        if endpoint is None or token is None:
            endpoint, token = credentials()
        endpoint = endpoint.rstrip("/")
        self.base = endpoint if endpoint.endswith("/api2/json") else f"{endpoint}/api2/json"
        self.token = token
        cafile = os.getenv("SSL_CERT_FILE") or None
        context = ssl.create_default_context(cafile=cafile)
        self.opener = build_opener(HTTPSHandler(context=context))

    def get(self, path: str):
        request = Request(
            f"{self.base}{path}",
            headers={"Authorization": f"PVEAPIToken={self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                body = json.loads(response.read())
        except HTTPError as error:
            raise ProxmoxError(f"Proxmox GET {path} devolvió HTTP {error.code}", error.code) from error
        except (URLError, TimeoutError, ValueError) as error:
            raise ProxmoxError(f"Proxmox GET {path} falló: {error}") from error
        return body.get("data")
    def inventory(self) -> list[dict]:
        return self.get("/cluster/resources?type=vm") or []

    def vm_config(self, node: str, vmid: int) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config?current=1") or {}

    def storage_status(self, node: str, storage: str) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/storage/{quote(storage, safe='')}/status") or {}
