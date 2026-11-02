import json
import os
import re
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener, HTTPSHandler
from typing import Callable

from api.targets import EnvironmentError, proxmox_credentials


class ProxmoxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, *, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def credentials(environment: str = "lab") -> tuple[str, str]:
    try:
        return proxmox_credentials(environment)
    except EnvironmentError as error:
        raise ProxmoxError(error.code) from error


def load_environment(environment: str = "lab") -> tuple[str, str]:
    endpoint, token = credentials(environment)
    os.environ["PROXMOX_VE_ENDPOINT"] = endpoint
    os.environ["PROXMOX_VE_API_TOKEN"] = token
    return endpoint, token


class ProxmoxClient:
    def __init__(self, endpoint: str | None = None, token: str | None = None, *, environment: str = "lab"):
        if endpoint is None or token is None:
            endpoint, token = credentials(environment)
        endpoint = endpoint.rstrip("/")
        self.base = endpoint if endpoint.endswith("/api2/json") else f"{endpoint}/api2/json"
        self.token = token
        cafile = os.getenv("SSL_CERT_FILE") or None
        context = ssl.create_default_context(cafile=cafile)
        self.opener = build_opener(HTTPSHandler(context=context))
        self._heartbeat_callback: Callable[[], None] | None = None

    def set_heartbeat(self, callback: Callable[[], None] | None) -> None:
        self._heartbeat_callback = callback

    def _heartbeat(self) -> None:
        callback = getattr(self, "_heartbeat_callback", None)
        if callback is not None:
            callback()

    def _request(
        self,
        method: str,
        path: str,
        parameters: dict | None = None,
        *,
        uncertain: bool = False,
        timeout: float | None = None,
        attempts: int | None = None,
    ):
        data = urlencode(parameters).encode() if parameters else None
        headers = {
            "Authorization": f"PVEAPIToken={self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(
            f"{self.base}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        # Reads are safe to retry when the Docker/Proxmox DNS path has a
        # transient failure. A mutating request is retried only when name
        # resolution failed before a connection could be opened; retrying a
        # timeout after a request was sent could duplicate a power/task call.
        attempts = max(
            1,
            int(
                attempts
                if attempts is not None
                else os.getenv("IACP_PROXMOX_RETRY_ATTEMPTS", "6")
            ),
        )
        backoff = max(0.1, float(os.getenv("IACP_PROXMOX_RETRY_BACKOFF", "0.5")))
        request_timeout = max(
            1.0,
            float(
                timeout
                if timeout is not None
                else os.getenv("IACP_PROXMOX_REQUEST_TIMEOUT", "20")
            ),
        )
        for attempt in range(attempts):
            try:
                with self.opener.open(request, timeout=request_timeout) as response:
                    body = json.loads(response.read())
                return body.get("data")
            except HTTPError as error:
                # A GET can be retried safely. A POST may already have
                # reached Proxmox even when it returns 5xx; retrying it here
                # could submit a second power/task mutation. The worker will
                # reconcile the physical state instead.
                if method == "GET" and 500 <= error.code < 600 and attempt + 1 < attempts:
                    time.sleep(min(8.0, backoff * (2**attempt)))
                    continue
                code = "POWER_PERMISSION_DENIED" if error.code in {401, 403} else "POWER_PROXMOX_UNAVAILABLE"
                raise ProxmoxError(code, error.code, uncertain=uncertain and method != "GET") from error
            except (URLError, TimeoutError, ValueError) as error:
                reason = getattr(error, "reason", None)
                message = str(error).lower()
                dns_failure = (
                    isinstance(error, URLError) and isinstance(reason, socket.gaierror)
                ) or any(
                    marker in message
                    for marker in (
                        "no address associated with hostname",
                        "temporary failure in name resolution",
                        "name or service not known",
                        "could not resolve host",
                    )
                )
                retryable = method == "GET" or dns_failure
                if not retryable or attempt + 1 >= attempts:
                    raise ProxmoxError(
                        f"Proxmox {method} {path} falló: {error}",
                        uncertain=uncertain,
                    ) from error
                time.sleep(min(8.0, backoff * (2**attempt)))

    def get(
        self,
        path: str,
        *,
        uncertain: bool = False,
        timeout: float | None = None,
        attempts: int | None = None,
    ):
        return self._request(
            "GET",
            path,
            uncertain=uncertain,
            timeout=timeout,
            attempts=attempts,
        )

    def post(self, path: str, parameters: dict | None = None):
        return self._request("POST", path, parameters, uncertain=True)

    def put(self, path: str, parameters: dict | None = None):
        return self._request("PUT", path, parameters, uncertain=True)

    def inventory(self, *, uncertain: bool = False) -> list[dict]:
        return self.get("/cluster/resources?type=vm", uncertain=uncertain) or []

    def pools(self) -> list[dict]:
        return self.get("/pools") or []

    def nodes(self) -> list[dict]:
        return self.get("/nodes") or []

    def vm_config(self, node: str, vmid: int) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config?current=1") or {}

    def ensure_network_model(self, node: str, vmid: int, name: str, model: str) -> dict:
        """Rewrite the primary NIC after clone so Proxmox regenerates cloud-init.

        The AlmaLinux 9 Golden Image can retain the cloned virtio/cloud-init
        ordering when a larger kvm plan is created. Reapplying the final NIC
        model while the VM is still stopped makes the generated network seed
        and the QEMU device converge before the first boot.
        """

        if model not in {"virtio", "e1000"}:
            raise ProxmoxError("NETWORK_MODEL_INVALID")
        config = self.vm_config(node, vmid)
        if (
            config.get("name") != name
            or config.get("template", 0)
            or not isinstance(config.get("net0"), str)
        ):
            raise ProxmoxError("POWER_IDENTITY_MISMATCH")
        net0 = config["net0"]
        first, *rest = net0.split(",", 1)
        if "=" not in first:
            raise ProxmoxError("NETWORK_CONFIG_INVALID")
        _, address = first.split("=", 1)
        desired = f"{model}={address}"
        if rest:
            desired += f",{rest[0]}"
        self.put(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config",
            {"net0": desired},
        )
        return {"network_model": model, "net0": desired}

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

    def guest_agent_interfaces(self, node: str, vmid: int) -> list[dict]:
        """Read the guest network interfaces without claiming connectivity."""

        # Guest Agent calls can block while cloud-init or qga is converging.
        # Keep the public network observation responsive; callers can poll
        # again instead of waiting through the generic GET retry budget.
        data = self.get(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/network-get-interfaces",
            timeout=float(os.getenv("IACP_GUEST_AGENT_TIMEOUT", "8")),
            attempts=int(os.getenv("IACP_GUEST_AGENT_ATTEMPTS", "2")),
        ) or {}
        if isinstance(data, dict):
            result = data.get("result", [])
            return result if isinstance(result, list) else []
        return data if isinstance(data, list) else []

    @staticmethod
    def _recoverable_power_error(error: ProxmoxError) -> bool:
        message = str(error).lower()
        return error.uncertain or any(
            marker in message
            for marker in (
                "power_task_failed",
                "power_proxmox_unavailable",
                "can't lock file",
                "got timeout",
                "lock-",
            )
        )

    @staticmethod
    def _power_recovery_delay(attempt: int) -> float:
        base = max(0.5, float(os.getenv("IACP_POWER_RECOVERY_BACKOFF", "2")))
        return min(15.0, base * (2**attempt))

    def _submit_power_action(self, node: str, vmid: int, action: str) -> dict:
        parameters = {}
        if action in {"shutdown", "reboot", "stop"}:
            parameters["timeout"] = 60
        if action == "shutdown":
            parameters["forceStop"] = 0
        task = self.post(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/status/{action}",
            parameters,
        )
        self._validate_task(node, vmid, action, task)
        return {"action": action, "task": task, **self.wait_task(node, vmid, action, task)}

    def _reconcile_power_action(
        self,
        node: str,
        vmid: int,
        name: str,
        action: str,
        initial_error: ProxmoxError,
    ) -> dict:
        """Recover a power operation from a lost task poll or a Proxmox lock."""

        deadline = time.monotonic() + max(
            5.0, float(os.getenv("IACP_POWER_RECOVERY_TIMEOUT", "300"))
        )
        last_error = initial_error
        attempt = 0
        while time.monotonic() < deadline:
            self._heartbeat()
            try:
                observed = self.power_state(node, vmid, name)
            except ProxmoxError as error:
                if not self._recoverable_power_error(error):
                    raise
                last_error = error
                observed = None

            current = observed.get("power_state") if observed else None
            desired = "running" if action == "start" else "stopped"
            if action != "reboot" and current == desired:
                return {
                    "action": action,
                    "recovered": True,
                    "recovery_error": str(initial_error)[:256],
                    **observed,
                }

            try:
                if action == "start" and current == "stopped":
                    return {
                        **self._submit_power_action(node, vmid, "start"),
                        "recovered": True,
                        "recovery_error": str(initial_error)[:256],
                    }
                if action in {"shutdown", "stop"} and current == "running":
                    result = self._submit_power_action(node, vmid, "stop")
                    return {
                        "action": action,
                        "fallback_action": "stop",
                        "recovered": True,
                        "recovery_error": str(initial_error)[:256],
                        **result,
                    }
                if action == "reboot" and current in {"running", "stopped"}:
                    stopped = (
                        {"power_state": "stopped", "already": True}
                        if current == "stopped"
                        else self._submit_power_action(node, vmid, "stop")
                    )
                    started = self._submit_power_action(node, vmid, "start")
                    return {
                        "action": action,
                        "fallback_action": "stop_start",
                        "recovered": True,
                        "recovery_error": str(initial_error)[:256],
                        "stop": stopped,
                        "start": started,
                        "power_state": started.get("power_state", "running"),
                    }
            except ProxmoxError as error:
                if not self._recoverable_power_error(error):
                    raise
                last_error = error

            time.sleep(self._power_recovery_delay(attempt))
            attempt += 1

        raise ProxmoxError(
            f"POWER_OPERATION_UNCERTAIN: {str(last_error)[:256]}",
            uncertain=True,
        ) from initial_error

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

        try:
            return {"action": action, **self._submit_power_action(node, vmid, action)}
        except ProxmoxError as error:
            # Some certified Golden Images have the QEMU agent enabled in
            # configuration before the guest agent is actually running. PVE
            # can then report an ACPI/qga timeout for qmshutdown. A failed
            # graceful shutdown is safe to complete with qmstop; this keeps
            # the public shutdown endpoint deterministic for destroy flows.
            if action == "shutdown" and str(error).startswith("POWER_TASK_FAILED"):
                return self._reconcile_power_action(node, vmid, name, action, error)
            # A guest can be running while its ACPI/QEMU reboot task times
            # out (for example while cloud-init or the guest agent is
            # wedged).  A stop/start is the deterministic equivalent for
            # the public reboot contract and avoids exposing a misleading
            # uncertain operation to API consumers.
            if self._recoverable_power_error(error):
                return self._reconcile_power_action(node, vmid, name, action, error)
            raise

    def stop(self, node: str, vmid: int, name: str) -> dict:
        """Force-stop only after a graceful shutdown task has failed."""

        self.permissions(node, vmid, mutate=True)
        observed = self.power_state(node, vmid, name)
        if observed["power_state"] == "stopped":
            return {"action": "stop", "already": True, **observed}
        try:
            return self._submit_power_action(node, vmid, "stop")
        except ProxmoxError as error:
            if self._recoverable_power_error(error):
                return self._reconcile_power_action(node, vmid, name, "stop", error)
            raise

    @staticmethod
    def _validate_task(node: str, vmid: int, action: str, task: str) -> None:
        task_type = {
            "start": "qmstart",
            "shutdown": "qmshutdown",
            "reboot": "qmreboot",
            "stop": "qmstop",
        }[action]
        pattern = (
            rf"UPID:{re.escape(node)}:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:"
            rf"{task_type}:{vmid}:[^:\r\n]+:"
        )
        if not isinstance(task, str) or len(task) > 512 or not re.fullmatch(pattern, task):
            raise ProxmoxError("POWER_TASK_IDENTITY_MISMATCH")

    def wait_task(self, node: str, vmid: int, action: str, task: str) -> dict:
        timeout = int(os.getenv("IACP_POWER_TIMEOUT", "180"))
        deadline = time.monotonic() + timeout
        poll_attempt = 0
        while True:
            self._heartbeat()
            try:
                data = self.get(
                    f"/nodes/{quote(node, safe='')}/tasks/{quote(task, safe='')}/status",
                    uncertain=True,
                ) or {}
            except ProxmoxError as error:
                if not error.uncertain:
                    raise
                if time.monotonic() >= deadline:
                    raise ProxmoxError("POWER_TASK_UNCERTAIN", uncertain=True) from error
                time.sleep(min(8.0, 0.5 * (2**poll_attempt)))
                poll_attempt += 1
                continue
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
            raise ProxmoxError(f"POWER_TASK_FAILED: {exitstatus or 'unknown'}")


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
