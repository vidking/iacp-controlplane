"""Bounded, single-pass Guest Initialization for technical root access."""

import base64
import os
import socket
import time

import paramiko

from worker.proxmox import ProxmoxError


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return value


def _ssh_port_ready(address: str) -> bool:
    try:
        with socket.create_connection((address, 22), timeout=1):
            return True
    except OSError:
        return False


def _wait_guest(client, node: str, vmid: int, command: str, args: list[str] | None = None,
                input_data: str | None = None) -> tuple[int, str, str]:
    pid = client.guest_exec(node, vmid, command, args or [], input_data=input_data)
    deadline = time.monotonic() + int(os.getenv("IACP_GUEST_INIT_TIMEOUT", "180"))
    while True:
        result = client.guest_exec_status(node, vmid, pid)
        if result.get("exited"):
            return int(result.get("exitcode", 1)), _decode(result.get("out-data")), _decode(result.get("err-data"))
        if time.monotonic() >= deadline:
            raise RuntimeError("GUEST_INITIALIZATION_TIMEOUT")
        time.sleep(1)


def _wait_for_qga(client, node: str, vmid: int, *, deadline: float | None = None):
    """Espera QGA estable sin reenviar ningún guest-exec."""
    deadline = deadline or time.monotonic() + int(os.getenv("IACP_GUEST_INIT_TIMEOUT", "180"))
    required_checks = max(1, int(os.getenv("IACP_GUEST_QGA_STABLE_CHECKS", "3")))
    stabilization = max(0.0, float(os.getenv("IACP_GUEST_QGA_STABILIZATION_SECONDS", "2")))
    stable_checks = 0
    while True:
        try:
            result = client.guest_network_interfaces(node, vmid)
        except ProxmoxError as error:
            if str(error) != "POWER_PROXMOX_UNAVAILABLE":
                raise
            stable_checks = 0
        else:
            interfaces = result.get("result", result) if isinstance(result, dict) else result
            has_ipv4 = any(
                address.get("ip-address-type") == "ipv4"
                and not address.get("ip-address", "").startswith("127.")
                for interface in interfaces or []
                for address in interface.get("ip-addresses", [])
            )
            addresses = [
                address.get("ip-address", "")
                for interface in interfaces or []
                for address in interface.get("ip-addresses", [])
                if address.get("ip-address-type") == "ipv4"
                and not address.get("ip-address", "").startswith("127.")
            ]
            ssh_ready = bool(addresses) and _ssh_port_ready(addresses[0])
            agent_info = client.guest_agent_info(node, vmid)
            commands = {
                item.get("name")
                for item in (agent_info.get("result", {}) or {}).get("supported_commands", [])
                if item.get("enabled", True)
            }
            client.guest_agent_ping(node, vmid)
            exec_ready = {"guest-exec", "guest-exec-status"}.issubset(commands)
            stable_checks = stable_checks + 1 if has_ipv4 and ssh_ready and exec_ready else 0
            if stable_checks >= required_checks:
                if time.monotonic() + stabilization > deadline:
                    raise RuntimeError("GUEST_AGENT_NOT_READY")
                time.sleep(stabilization)
                return result
        if time.monotonic() >= deadline:
            raise RuntimeError("GUEST_AGENT_NOT_READY")
        time.sleep(1)


def _ip_address(client, node: str, vmid: int) -> str:
    deadline = time.monotonic() + int(os.getenv("IACP_GUEST_INIT_TIMEOUT", "180"))
    result = _wait_for_qga(client, node, vmid, deadline=deadline)
    interfaces = result.get("result", result) if isinstance(result, dict) else result
    for interface in interfaces or []:
        for address in interface.get("ip-addresses", []):
            value = address.get("ip-address", "")
            if address.get("ip-address-type") == "ipv4" and not value.startswith("127."):
                return value
    raise RuntimeError("GUEST_NETWORK_ADDRESS_UNAVAILABLE")


def _host_key(client, node: str, vmid: int) -> paramiko.PKey:
    code, stdout, _ = _wait_guest(
        client,
        node,
        vmid,
        "/bin/cat",
        ["/etc/ssh/ssh_host_ed25519_key.pub"],
    )
    if code != 0:
        raise RuntimeError("GUEST_SSH_HOST_KEY_UNAVAILABLE")
    entries = paramiko.hostkeys.HostKeyEntry.from_line(stdout.strip())
    if entries is None or not entries.key:
        raise RuntimeError("GUEST_SSH_HOST_KEY_INVALID")
    return entries.key


def _verify_root_ssh(client, node: str, vmid: int, password: str) -> None:
    address = _ip_address(client, node, vmid)
    host_key = _host_key(client, node, vmid)
    ssh = paramiko.SSHClient()
    ssh.get_host_keys().add(address, host_key.get_name(), host_key)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        ssh.connect(
            address,
            username="root",
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )
        _, stdout, _ = ssh.exec_command("/usr/bin/id -u", timeout=15)
        if stdout.read().decode("utf-8", errors="replace").strip() != "0":
            raise RuntimeError("ROOT_SSH_UID_INVALID")
    except paramiko.AuthenticationException as error:
        raise RuntimeError("ROOT_SSH_AUTHENTICATION_FAILED") from error
    except (paramiko.SSHException, OSError) as error:
        raise RuntimeError("ROOT_SSH_VERIFICATION_FAILED") from error
    finally:
        ssh.close()


def initialize_access(client, *, node: str, vmid: int, name: str, password: str) -> None:
    if not password or "\n" in password or "\r" in password:
        raise RuntimeError("ROOT_PASSWORD_INVALID")
    _wait_for_qga(client, node, vmid)
    code, _, _ = _wait_guest(
        client,
        node,
        vmid,
        "/usr/bin/cloud-init",
        ["status", "--wait"],
    )
    if code != 0:
        raise RuntimeError("CLOUD_INIT_NOT_READY")
    script = (
        "set -eu; "
        "export PATH=/usr/sbin:/usr/bin:/sbin:/bin; "
        "install -d -m 0755 /run/sshd; "
        "/usr/sbin/chpasswd; "
        "install -d -m 0755 /etc/ssh/sshd_config.d; "
        "printf '%s\\n' 'PermitRootLogin yes' 'PasswordAuthentication yes' "
        "> /etc/ssh/sshd_config.d/99-iacp-root-access.conf; "
        "/usr/sbin/sshd -t; "
        "(systemctl reload ssh || systemctl reload sshd || service ssh reload)"
    )
    code, _, _ = _wait_guest(
        client,
        node,
        vmid,
        "/bin/sh",
        ["-c", script],
        input_data=f"root:{password}\n",
    )
    if code != 0:
        raise RuntimeError("GUEST_INITIALIZATION_PROGRAM_FAILED")
    _verify_root_ssh(client, node, vmid, password)
