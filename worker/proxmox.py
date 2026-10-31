import json
import os
import re
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPSHandler


class ProxmoxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, *, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def credentials(*, readonly: bool = False) -> tuple[str, str]:
    endpoint_env = os.getenv("IACP_PROXMOX_ENDPOINT") or os.getenv("PROXMOX_VE_ENDPOINT")
    token_env = os.getenv("IACP_PROXMOX_API_TOKEN") or os.getenv("PROXMOX_VE_API_TOKEN")
    if endpoint_env or token_env:
        if not endpoint_env or not token_env:
            raise ProxmoxError("credencial Proxmox por entorno incompleta")
        return str(endpoint_env).rstrip("/"), str(token_env)

    path_variable = "IACP_PROXMOX_READONLY_CREDENTIALS_FILE" if readonly else "IACP_PROXMOX_CREDENTIALS_FILE"
    default_path = "/run/secrets/proxmox_readonly_credentials" if readonly else "/run/secrets/proxmox_credentials"
    path = os.getenv(path_variable, default_path)
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
    def __init__(self, endpoint: str | None = None, token: str | None = None, *, readonly: bool = False):
        if endpoint is None or token is None:
            endpoint, token = credentials(readonly=readonly)
        endpoint = endpoint.rstrip("/")
        self.base = endpoint if endpoint.endswith("/api2/json") else f"{endpoint}/api2/json"
        self.token = token
        cafile = os.getenv("SSL_CERT_FILE") or None
        context = ssl.create_default_context(cafile=cafile)
        self.opener = build_opener(HTTPSHandler(context=context))

    def _request(self, method: str, path: str, parameters: dict | None = None, *, uncertain: bool = False):
        data = urlencode(parameters, doseq=True).encode() if parameters else None
        url = f"{self.base}{path}"
        if method == "GET" and data is not None:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{data.decode()}"
            data = None
        headers = {
            "Authorization": f"PVEAPIToken={self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                body = json.loads(response.read())
        except HTTPError as error:
            code = "POWER_PERMISSION_DENIED" if error.code in {401, 403} else "POWER_PROXMOX_UNAVAILABLE"
            raise ProxmoxError(code, error.code) from error
        except (URLError, TimeoutError, ValueError) as error:
            raise ProxmoxError(
                f"Proxmox {method} {path} falló: {error}",
                uncertain=uncertain,
            ) from error
        return body.get("data")

    def get(self, path: str, parameters: dict | None = None, *, uncertain: bool = False):
        return self._request("GET", path, parameters, uncertain=uncertain)

    def post(self, path: str, parameters: dict | None = None):
        return self._request("POST", path, parameters, uncertain=True)

    def inventory(self) -> list[dict]:
        return self.get("/cluster/resources?type=vm") or []

    def guest_exec(self, node: str, vmid: int, command: str, args: list[str], *, input_data: str | None = None) -> str:
        parameters: dict[str, object] = {"command": command}
        if args:
            parameters["arg"] = args
        if input_data is not None:
            parameters["input-data"] = input_data
        result = self.post(f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/exec", parameters)
        if not isinstance(result, str):
            raise ProxmoxError("GUEST_EXEC_DISPATCH_FAILED", uncertain=True)
        return result

    def guest_exec_status(self, node: str, vmid: int, pid: str) -> dict:
        return self.get(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/exec-status",
            parameters={"pid": pid},
        ) or {}

    def guest_network_interfaces(self, node: str, vmid: int) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/network-get-interfaces") or {}

    def guest_agent_info(self, node: str, vmid: int) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/info") or {}

    def guest_agent_ping(self, node: str, vmid: int) -> dict:
        """Comprueba el RPC QGA sin ejecutar comandos dentro del invitado."""
        return self.post(f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/ping") or {}

    def vm_config(self, node: str, vmid: int) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config?current=1") or {}

    def storage_status(self, node: str, storage: str) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/storage/{quote(storage, safe='')}/status") or {}

    def permissions(self, node: str, vmid: int, *, mutate: bool = False) -> None:
        path = f"/vms/{vmid}"
        data = self.get("/access/permissions?" + urlencode({"path": path}))
        privileges = data.get(path) if isinstance(data, dict) else None
        required = ["VM.Audit", "VM.PowerMgmt"] if mutate else ["VM.Audit"]
        if not isinstance(privileges, dict) or any(privileges.get(name) not in {0, 1} for name in required):
            raise ProxmoxError("POWER_PERMISSION_DENIED", 403)

    def power_state(self, node: str, vmid: int, name: str) -> dict:
        config = self.vm_config(node, vmid)
        status = self.get(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/status/current"
        ) or {}
        if (
            config.get("name") != name
            or config.get("template", 0)
            or status.get("vmid") != vmid
            or status.get("status") not in {"running", "stopped"}
        ):
            raise ProxmoxError("POWER_IDENTITY_MISMATCH")
        return {
            "power_state": status["status"],
            "observed_at": now_iso(),
            "qmp_status": status.get("qmpstatus"),
            "uptime": status.get("uptime"),
        }

    def power(self, node: str, vmid: int, name: str, action: str) -> dict:
        if action not in {"start", "shutdown", "reboot"}:
            raise ProxmoxError("POWER_ACTION_INVALID")
        self.permissions(node, vmid, mutate=True)
        observed = self.power_state(node, vmid, name)
        current = observed["power_state"]
        if action == "start" and current == "running":
            return {"action": action, "already": True, **observed}
        if action == "shutdown" and current == "stopped":
            return {"action": action, "already": True, **observed}
        if action == "reboot" and current != "running":
            raise ProxmoxError("POWER_STATE_INVALID")

        parameters = {}
        if action in {"shutdown", "reboot"}:
            parameters = {"timeout": 60}
        if action == "shutdown":
            parameters["forceStop"] = 0
        task = self.post(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/status/{action}",
            parameters,
        )
        self._validate_task(node, vmid, action, task)
        result = self.wait_task(node, vmid, action, task)
        return {"action": action, "task": task, **result}

    @staticmethod
    def _validate_task(node: str, vmid: int, action: str, task: str) -> None:
        task_type = {"start": "qmstart", "shutdown": "qmshutdown", "reboot": "qmreboot"}[action]
        pattern = (
            rf"UPID:{re.escape(node)}:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:"
            rf"{task_type}:{vmid}:[^:\r\n]+:"
        )
        if not isinstance(task, str) or len(task) > 512 or not re.fullmatch(pattern, task):
            raise ProxmoxError("POWER_TASK_IDENTITY_MISMATCH")

    def wait_task(self, node: str, vmid: int, action: str, task: str) -> dict:
        timeout = int(os.getenv("IACP_POWER_TIMEOUT", "180"))
        deadline = time.monotonic() + timeout
        while True:
            data = self.get(
                f"/nodes/{quote(node, safe='')}/tasks/{quote(task, safe='')}/status",
                uncertain=True,
            ) or {}
            state = data.get("status")
            if state == "running":
                if time.monotonic() >= deadline:
                    raise ProxmoxError("POWER_TASK_UNCERTAIN", uncertain=True)
                time.sleep(1)
                continue
            if state != "stopped":
                raise ProxmoxError("POWER_PROXMOX_UNAVAILABLE", uncertain=True)
            exitstatus = data.get("exitstatus")
            if exitstatus == "OK":
                final = "running" if action in {"start", "reboot"} else "stopped"
                return {"power_state": final, "exitstatus": exitstatus}
            raise ProxmoxError("POWER_TASK_FAILED")


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
