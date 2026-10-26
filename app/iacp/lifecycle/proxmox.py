"""Small HTTPS client with one allowlisted mutation surface and no POST retries."""
from dataclasses import dataclass
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from ..models import utcnow
from ..terraform.observation import NoRedirect


class PowerError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Target:
    vmid: int
    node: str
    name: str

    def __post_init__(self):
        if (type(self.vmid) is not int or self.vmid < 100 or self.vmid == 104
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,127}", self.node) or not self.name):
            raise PowerError("POWER_TARGET_INVALID")

    @property
    def path(self):
        return f"/nodes/{quote(self.node, safe='')}/qemu/{self.vmid}"


def readiness(power_state):
    return {"power_state": power_state, "observed_at": utcnow().isoformat(),
            "os_boot": "unknown", "guest_agent": "unknown", "cloud_init": "unknown",
            "accessible": "unknown", "guest_ready": None}


def granted(privileges, name):
    # GET /access/permissions maps an effective privilege to its propagation
    # flag. Both 0 and 1 grant THIS exact path; absence denies the privilege.
    # Do not use this helper to infer permissions on descendants.
    value = privileges.get(name)
    return type(value) is int and value in (0, 1)


class ProxmoxPower:
    timeout = 10
    acpi_timeout = 60

    @classmethod
    def configured(cls):
        try:
            credentials = json.loads(Path(os.environ["IACP_KVM_CREDENTIALS_FILE"]).read_text())
            if set(credentials) != {"endpoint", "api_token"}:
                raise ValueError()
            endpoint, token = credentials["endpoint"], credentials["api_token"]
            url = urlsplit(endpoint)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.path not in {"", "/"}
                    or not isinstance(token, str) or not token or "\n" in token or "\r" in token):
                raise ValueError()
            context = ssl.create_default_context(cafile=os.environ.get("IACP_KVM_CA_FILE"))
            opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
            return cls(endpoint.rstrip("/"), token, opener)
        except (KeyError, OSError, ValueError, TypeError, AttributeError):
            raise PowerError("POWER_CREDENTIALS_UNAVAILABLE") from None

    def __init__(self, endpoint, token, opener):
        self.endpoint, self.token, self.opener = endpoint, token, opener

    def _request(self, method, path, parameters=None):
        headers = {"Authorization": "PVEAPIToken=" + self.token}
        data = None
        if parameters is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            data = urlencode(parameters).encode()
        try:
            request = Request(self.endpoint + "/api2/json" + path, data=data, headers=headers, method=method)
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise PowerError("POWER_PROXMOX_UNAVAILABLE")
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError()
            return json.loads(body)["data"]
        except HTTPError as failure:
            raise PowerError("POWER_PERMISSION_DENIED" if failure.code in {401, 403} else "POWER_PROXMOX_UNAVAILABLE") from None
        except (OSError, URLError, HTTPException, ValueError, TypeError, KeyError):
            raise PowerError("POWER_PROXMOX_UNAVAILABLE") from None

    def permissions(self, target, *, mutate=False):
        path = f"/vms/{target.vmid}"
        data = self._request("GET", "/access/permissions?" + urlencode({"path": path}))
        if not isinstance(data, dict) or not isinstance(data.get(path), dict):
            raise PowerError("POWER_PERMISSION_DENIED")
        if not granted(data[path], "VM.Audit") or (mutate and not granted(data[path], "VM.PowerMgmt")):
            raise PowerError("POWER_PERMISSION_DENIED")

    def observe(self, target, *, preflight=False):
        config = self._request("GET", target.path + "/config?current=1")
        status = self._request("GET", target.path + "/status/current")
        if (not isinstance(config, dict) or not isinstance(status, dict)
                or config.get("name") != target.name or config.get("template", 0)
                or status.get("vmid") != target.vmid or status.get("status") not in {"running", "stopped"}):
            raise PowerError("POWER_IDENTITY_MISMATCH")
        if preflight:
            if config.get("lock"):
                raise PowerError("POWER_RESOURCE_BUSY")
            if status.get("ha", {}).get("managed", 0):
                raise PowerError("POWER_HA_UNSUPPORTED")
            if status.get("qmpstatus", status["status"]) not in {"running", "stopped"}:
                raise PowerError("POWER_VM_PAUSED")
            pending = self._request("GET", target.path + "/pending")
            if not isinstance(pending, list) or any(not isinstance(item, dict) for item in pending):
                raise PowerError("POWER_PROXMOX_UNAVAILABLE")
            if any("pending" in item or item.get("delete") for item in pending):
                raise PowerError("POWER_PENDING_CONFIGURATION")
        return readiness(status["status"])

    def submit(self, target, action):
        if action not in {"start", "shutdown", "reboot"}:
            raise PowerError("POWER_ACTION_INVALID")
        parameters = {} if action == "start" else {"timeout": self.acpi_timeout}
        if action == "shutdown":
            parameters["forceStop"] = 0
        task = self._request("POST", target.path + "/status/" + action, parameters)
        self.validate_task(target, action, task)
        return task

    @staticmethod
    def validate_task(target, action, task):
        task_type = {"start": "qmstart", "shutdown": "qmshutdown", "reboot": "qmreboot"}[action]
        pattern = rf"UPID:{re.escape(target.node)}:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:{task_type}:{target.vmid}:[^:\r\n]+:"
        if not isinstance(task, str) or len(task) > 512 or not re.fullmatch(pattern, task):
            raise PowerError("POWER_TASK_IDENTITY_MISMATCH")

    def task_status(self, target, action, task):
        self.validate_task(target, action, task)
        data = self._request("GET", f"/nodes/{quote(target.node, safe='')}/tasks/{quote(task, safe='')}/status")
        if not isinstance(data, dict) or data.get("status") not in {"running", "stopped"}:
            raise PowerError("POWER_PROXMOX_UNAVAILABLE")
        if data["status"] == "running":
            return "running"
        exitstatus = data.get("exitstatus")
        if not isinstance(exitstatus, str):
            raise PowerError("POWER_PROXMOX_UNAVAILABLE")
        if exitstatus == "OK":
            return "completed"
        # Proxmox includes the decisive guest-side cause in the UPID exit
        # status. Preserve that distinction so a missing QEMU Guest Agent is
        # not reported as an opaque ACPI timeout by the Lifecycle state
        # machine. The command was consumed and must never be replayed.
        normalized = exitstatus.lower()
        if "qemu guest agent" in normalized and "timeout" in normalized:
            return "guest_agent_timeout"
        return "timeout" if "timeout" in normalized else "failed"
