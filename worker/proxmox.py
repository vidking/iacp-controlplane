import base64
import json
import os
import re
import socket
import ssl
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from typing import Callable


class ProxmoxError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, *, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        self.uncertain = uncertain


def credentials() -> tuple[str, str]:
    endpoint_env = os.getenv("IACP_PROXMOX_ENDPOINT") or os.getenv("PROXMOX_VE_ENDPOINT")
    token_env = os.getenv("IACP_PROXMOX_API_TOKEN") or os.getenv("PROXMOX_VE_API_TOKEN")
    if endpoint_env or token_env:
        if not endpoint_env or not token_env:
            raise ProxmoxError("credencial Proxmox por entorno incompleta")
        return str(endpoint_env).rstrip("/"), str(token_env)

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


class RejectRedirects(HTTPRedirectHandler):
    """Never forward authenticated requests to a redirected URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProxmoxError("PROXMOX_HTTPS_ENDPOINT_REQUIRED")


class ProxmoxClient:
    def __init__(self, endpoint: str | None = None, token: str | None = None):
        if endpoint is None or token is None:
            endpoint, token = credentials()
        endpoint = endpoint.rstrip("/")
        validate_endpoint(endpoint)
        self.base = endpoint if endpoint.endswith("/api2/json") else f"{endpoint}/api2/json"
        self.token = token
        cafile = os.getenv("SSL_CERT_FILE") or None
        context = ssl.create_default_context(cafile=cafile)
        self.opener = build_opener(HTTPSHandler(context=context), RejectRedirects())
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
        parameters: dict | list[tuple[str, str]] | None = None,
        *,
        uncertain: bool = False,
        timeout: float | None = None,
        attempts: int | None = None,
    ):
        if method != "GET":
            self._heartbeat()
        if parameters:
            # Proxmox declares ``sshkeys`` as a URL-encoded string inside the
            # form body. Encode the plain OpenSSH key here, at the HTTP
            # boundary, so callers never have to quote it themselves (and
            # cannot accidentally send raw reserved characters or encode it
            # twice before this form is serialized).
            items = (
                parameters.items() if isinstance(parameters, dict) else parameters
            )
            form_parameters = []
            for name, value in items:
                if name == "sshkeys" and value is not None:
                    if not isinstance(value, str) or "\r" in value or "\x00" in value:
                        raise ProxmoxError("PROXMOX_SSH_KEYS_INVALID")
                    value = quote(value, safe="")
                form_parameters.append((name, value))
            data = urlencode(form_parameters).encode()
        else:
            data = None
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
            0.1 if timeout is not None else 1.0,
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
                if not isinstance(body, dict) or "data" not in body:
                    raise ValueError("respuesta Proxmox sin data")
                return body["data"]
            except HTTPError as error:
                error.close()  # libera el socket; el código ya está en ``error.code``
                # A GET can be retried safely. A POST may already have
                # reached Proxmox even when it returns 5xx; retrying it here
                # could submit a second power/task mutation. The worker will
                # reconcile the physical state instead.
                if method == "GET" and 500 <= error.code < 600 and attempt + 1 < attempts:
                    time.sleep(min(8.0, backoff * (2**attempt)))
                    continue
                code = "POWER_PERMISSION_DENIED" if error.code in {401, 403} else "POWER_PROXMOX_UNAVAILABLE"
                # A 4xx response is an explicit rejection: Proxmox did not
                # accept the operation. Only server errors may have happened
                # after a mutation was applied and therefore remain uncertain.
                request_uncertain = uncertain and method != "GET" and (
                    error.code >= 500 or error.code == 408
                )
                raise ProxmoxError(code, error.code, uncertain=request_uncertain) from error
            except (OSError, HTTPException, ValueError) as error:
                # ``urllib`` solo envuelve en URLError los fallos de envío: una
                # conexión cortada al leer la respuesta llega sin envolver.
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

    def post(self, path: str, parameters: dict | list[tuple[str, str]] | None = None):
        return self._request("POST", path, parameters, uncertain=True)

    def put(self, path: str, parameters: dict | list[tuple[str, str]] | None = None):
        return self._request("PUT", path, parameters, uncertain=True)

    def inventory(
        self,
        *,
        uncertain: bool = False,
        timeout: float | None = None,
        attempts: int | None = None,
    ) -> list[dict]:
        data = self.get(
            "/cluster/resources?type=vm",
            uncertain=uncertain,
            timeout=timeout,
            attempts=attempts,
        )
        if not isinstance(data, list) or any(
            not isinstance(record, dict) or not str(record.get("vmid", "")).isdigit()
            for record in data
        ):
            raise ProxmoxError("PROXMOX_INVENTORY_UNCONFIRMED", uncertain=uncertain)
        if len({int(record["vmid"]) for record in data}) != len(data):
            raise ProxmoxError("PROXMOX_INVENTORY_UNCONFIRMED", uncertain=uncertain)
        return data

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

    def ensure_cloudinit_dns(
        self, node: str, vmid: int, name: str, servers: list[str]
    ) -> dict:
        """Converge the cloud-init resolver field before a static-WAN guest boots."""

        if servers != ["1.1.1.1", "8.8.8.8"]:
            raise ProxmoxError("CLOUDINIT_DNS_INVALID")
        config = self.vm_config(node, vmid)
        if (
            config.get("name") != name
            or config.get("template", 0)
            or not isinstance(config.get("net0"), str)
        ):
            raise ProxmoxError("CLOUDINIT_DNS_IDENTITY_MISMATCH")
        desired = " ".join(servers)
        if str(config.get("nameserver") or "").strip() != desired:
            state = self.power_state(node, vmid, name).get("power_state")
            if state != "stopped":
                raise ProxmoxError("PREBOOT_DNS_VM_ALREADY_RUNNING")
            self.put(
                f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config",
                {"nameserver": desired},
            )
        observed = self.vm_config(node, vmid)
        if (
            observed.get("name") != name
            or observed.get("template", 0)
            or str(observed.get("nameserver") or "").strip() != desired
        ):
            raise ProxmoxError("CLOUDINIT_DNS_UNVERIFIED", uncertain=True)
        return {"nameserver": desired}

    def ensure_cloudinit_password(
        self, node: str, vmid: int, name: str, username: str, password: str
    ) -> None:
        """Write the initial credential only to an identified, stopped VM."""
        if self.power_state(node, vmid, name)["power_state"] != "stopped":
            raise ProxmoxError("CLOUDINIT_PASSWORD_VM_ALREADY_RUNNING", uncertain=True)
        config = self.vm_config(node, vmid)
        if config.get("name") != name or config.get("template") or config.get("ciuser") != username:
            raise ProxmoxError("CLOUDINIT_PASSWORD_IDENTITY_MISMATCH", uncertain=True)
        path = f"/nodes/{quote(node, safe='')}/qemu/{vmid}/config"
        parameters = {"cipassword": password}
        if config.get("digest"):
            parameters["digest"] = config["digest"]
        self.put(path, parameters)
        observed = self.vm_config(node, vmid)
        if observed.get("name") != name or observed.get("ciuser") != username or not observed.get("cipassword"):
            raise ProxmoxError("CLOUDINIT_PASSWORD_UNCONFIRMED", uncertain=True)

    def storage_status(self, node: str, storage: str) -> dict:
        return self.get(f"/nodes/{quote(node, safe='')}/storage/{quote(storage, safe='')}/status") or {}

    def _privileges_granted(self, path: str, required: list[str]) -> bool:
        data = self.get("/access/permissions?" + urlencode({"path": path}))
        privileges = data.get(path) if isinstance(data, dict) else None
        return isinstance(privileges, dict) and all(
            privileges.get(name) in {True, 1} for name in required
        )

    def _pool_has_vm(self, pool_id: str, vmid: int) -> bool:
        data = self.get(f"/pools/{quote(pool_id, safe='')}")
        members = data.get("members") if isinstance(data, dict) else None
        if not isinstance(members, list):
            return False
        return any(
            isinstance(member, dict) and str(member.get("vmid", "")) == str(vmid)
            for member in members
        )

    def permissions(self, node: str, vmid: int, *, mutate: bool = False) -> None:
        """Exige los privilegios efectivos del token sobre la VM.

        En este Proxmox la ACL del orquestador se concede sobre el pool
        (``/pool/<id>``, propagate=1) y la consulta ``/access/permissions`` de
        ``/vms/<id>`` devuelve 0 para esos privilegios aunque Proxmox sí permita
        operar la VM por pertenecer al pool. Se acepta, por tanto, el privilegio
        en ``/vms/<id>`` o el heredado del pool configurado, pero solo si la VM
        es miembro de ese pool: una VM ajena sigue denegada.
        """

        required = ["VM.Audit", "VM.PowerMgmt"] if mutate else ["VM.Audit"]
        if self._privileges_granted(f"/vms/{vmid}", required):
            return
        pool_id = os.getenv("IACP_PROXMOX_POOL_ID", "iacp-lab").strip()
        if (
            re.fullmatch(r"[A-Za-z0-9_-]+", pool_id)
            and self._pool_has_vm(pool_id, vmid)
            and self._privileges_granted(f"/pool/{pool_id}", required)
        ):
            return
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

    def guest_agent_info(self, node: str, vmid: int) -> dict:
        return self.get(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/info",
            timeout=float(os.getenv("IACP_GUEST_AGENT_TIMEOUT", "8")),
            attempts=1,
        ) or {}

    def guest_agent_exec(
        self,
        node: str,
        vmid: int,
        command: list[str],
        *,
        input_data: str | bytes | None = None,
    ) -> dict:
        if not command or any(not isinstance(item, str) for item in command):
            raise ProxmoxError("GUEST_AGENT_COMMAND_INVALID")
        parameters = [("command", item) for item in command]
        if input_data is not None:
            if isinstance(input_data, str):
                input_data = input_data.encode()
            if not isinstance(input_data, bytes) or len(input_data) > 45 * 1024:
                raise ProxmoxError("GUEST_AGENT_INPUT_INVALID")
            parameters.append(
                ("input-data", base64.b64encode(input_data).decode("ascii"))
            )
        return self._request(
            "POST",
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/exec",
            parameters,
            uncertain=True,
            timeout=float(os.getenv("IACP_GUEST_AGENT_EXEC_TIMEOUT", "15")),
            attempts=1,
        ) or {}

    def guest_agent_exec_status(self, node: str, vmid: int, pid: int) -> dict:
        query = urlencode({"pid": int(pid)})
        return self.get(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/exec-status?{query}",
            timeout=float(os.getenv("IACP_GUEST_AGENT_TIMEOUT", "8")),
            attempts=1,
        ) or {}

    def guest_agent_file_read(self, node: str, vmid: int, path: str) -> dict:
        query = urlencode({"file": path, "count": 8192})
        return self.get(
            f"/nodes/{quote(node, safe='')}/qemu/{vmid}/agent/file-read?{query}",
            timeout=float(os.getenv("IACP_GUEST_AGENT_TIMEOUT", "8")),
            attempts=1,
        ) or {}

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
        try:
            result = self.wait_task(node, vmid, action, task)
        except ProxmoxError as error:
            error.task = task
            raise
        return {"action": action, "task": task, **result}

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
                    # La orden ya se envió: no observar no demuestra su resultado.
                    error.uncertain = True
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

            if attempt == 0 and str(initial_error).startswith("POWER_TASK_FAILED"):
                try:
                    # Un qmstart terminalmente fallido deja el VM apagado y
                    # su UPID confirma que la orden anterior ya terminó. En
                    # ese caso es seguro volver a intentar start una sola vez.
                    # Una respuesta perdida conserva ``uncertain=True`` y no
                    # entra aquí: nunca repetimos una mutación sin resultado.
                    task = getattr(initial_error, "task", None)
                    if (
                        action == "start"
                        and current == "stopped"
                        and isinstance(task, str)
                        and bool(task)
                        and not initial_error.uncertain
                    ):
                        result = self._submit_power_action(node, vmid, "start")
                        return {
                            **result,
                            "action": action,
                            "retry_after_failed_task": True,
                            "recovered": True,
                        }
                    if action == "shutdown" and current == "running":
                        result = self._submit_power_action(node, vmid, "stop")
                        return {
                            **result,
                            "action": action,
                            "fallback_action": "stop",
                            "recovered": True,
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
                            "stop": stopped,
                            "start": started,
                            "power_state": started["power_state"],
                        }
                except ProxmoxError as error:
                    raise ProxmoxError("POWER_FALLBACK_UNCONFIRMED", uncertain=True) from error

            # Una respuesta perdida solo autoriza volver a observar o volver
            # a consultar la misma tarea. Nunca autoriza otra mutación.
            task = getattr(initial_error, "task", None)
            if task and initial_error.uncertain:
                try:
                    result = self.wait_task(node, vmid, action, task)
                except ProxmoxError as error:
                    raise ProxmoxError("POWER_OPERATION_UNCERTAIN", uncertain=True) from error
                return {"action": action, "task": task, "recovered": True, **result}

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

    def completed_power_task_during(self, node, vmid, action, started_at, finished_at):
        """Demostrar una mutación previa sin repetirla ni usar solo el estado running."""
        from datetime import datetime

        try:
            begin = datetime.fromisoformat(started_at).timestamp()
            end = datetime.fromisoformat(finished_at).timestamp()
            if end < begin:
                return None
        except (TypeError, ValueError):
            return None
        owner = self.token.split("=", 1)[0]
        task_type = {
            "reboot": "qmreboot",
            "start": "qmstart",
            "shutdown": "qmshutdown",
        }[action]
        records = self.get(
            f"/nodes/{quote(node, safe='')}/tasks?"
            + urlencode({"vmid": vmid, "limit": 50, "typefilter": task_type})
        )
        matches = []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            identity = record.get("user", "") + (
                "!" + record["tokenid"] if record.get("tokenid") else ""
            )
            try:
                valid = (
                    record.get("node") == node
                    and str(record.get("id")) == str(vmid)
                    and record.get("type") == task_type
                    and identity == owner
                    and record.get("status") == "OK"
                    and begin - 1
                    <= float(record["starttime"])
                    <= float(record["endtime"])
                    <= end + 1
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            if valid:
                task = record.get("upid")
                self._validate_task(node, vmid, action, task)
                matches.append(task)
        if len(matches) != 1:
            return None
        self.wait_task(node, vmid, action, matches[0])
        return matches[0]

    def completed_clone_task_during(
        self, node, target_vmid, source_vmid, started_at, finished_at
    ) -> bool:
        """Prueba que este token clonó la Golden Image al VMID esperado.

        Proxmox identifica `qmclone` por la VM fuente en el UPID. El VMID de
        destino se prueba con la línea de log generada por la propia tarea.
        Una coincidencia ambigua, incompleta o ajena al intervalo de la
        operación se rechaza.
        """
        from datetime import datetime

        try:
            begin = datetime.fromisoformat(started_at).timestamp()
            end = datetime.fromisoformat(finished_at).timestamp()
            if end < begin:
                return False
        except (TypeError, ValueError):
            return False

        owner = self.token.split("=", 1)[0]
        records = self.get(
            f"/nodes/{quote(node, safe='')}/tasks?"
            + urlencode(
                {
                    "since": max(0, int(begin - 1)),
                    "limit": 500,
                    "typefilter": "qmclone",
                }
            )
        )
        expected_line = f"creating a clone of VM {int(source_vmid)} with ID {int(target_vmid)}"
        pattern = (
            rf"UPID:{re.escape(node)}:[0-9A-Fa-f]+:[0-9A-Fa-f]+:[0-9A-Fa-f]+:"
            rf"qmclone:{int(source_vmid)}:[^:\r\n]+:"
        )
        matches = []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            identity = record.get("user", "") + (
                "!" + record["tokenid"] if record.get("tokenid") else ""
            )
            try:
                valid = (
                    record.get("node") == node
                    and str(record.get("id")) == str(source_vmid)
                    and record.get("type") == "qmclone"
                    and identity == owner
                    and record.get("status") == "OK"
                    and begin - 1
                    <= float(record["starttime"])
                    <= float(record["endtime"])
                    <= end + 1
                )
            except (KeyError, TypeError, ValueError):
                valid = False
            task = record.get("upid")
            if not valid or not isinstance(task, str) or not re.fullmatch(pattern, task):
                continue

            log = self.get(
                f"/nodes/{quote(node, safe='')}/tasks/{quote(task, safe='')}/log?limit=100"
            )
            log_lines = log if isinstance(log, list) else []
            if not any(
                isinstance(line, dict) and line.get("t") == expected_line
                for line in log_lines
            ):
                continue
            status = self.get(
                f"/nodes/{quote(node, safe='')}/tasks/{quote(task, safe='')}/status",
                uncertain=True,
            ) or {}
            if status.get("status") == "stopped" and status.get("exitstatus") == "OK":
                matches.append(task)

        return len(matches) == 1

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
