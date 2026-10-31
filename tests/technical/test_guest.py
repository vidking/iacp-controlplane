import pytest

from worker import guest
from worker.proxmox import ProxmoxError


class QgaClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.network_calls = 0
        self.exec_calls = []

    def guest_network_interfaces(self, node, vmid):
        self.network_calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def guest_exec(self, node, vmid, command, args, *, input_data=None):
        self.exec_calls.append((command, args, input_data))
        return f"pid-{len(self.exec_calls)}"

    def guest_exec_status(self, node, vmid, pid):
        return {"exited": True, "exitcode": 0}


def interfaces(ip=None):
    addresses = [] if ip is None else [{"ip-address-type": "ipv4", "ip-address": ip}]
    return {"result": [{"name": "eth0", "ip-addresses": addresses}]}


def test_qga_readiness_retries_without_guest_exec(monkeypatch):
    client = QgaClient([
        ProxmoxError("POWER_PROXMOX_UNAVAILABLE", 500),
        ProxmoxError("POWER_PROXMOX_UNAVAILABLE", 500),
        interfaces("192.0.2.10"),
    ])
    monkeypatch.setattr(guest.time, "sleep", lambda _seconds: None)

    assert guest._ip_address(client, "mng01", 30000) == "192.0.2.10"
    assert client.network_calls == 3
    assert client.exec_calls == []


def test_initialize_access_waits_for_qga_before_single_pass_guest_exec(monkeypatch):
    client = QgaClient([interfaces("192.0.2.10")])
    verified = []
    monkeypatch.setattr(guest, "_verify_root_ssh", lambda *args: verified.append(args))

    guest.initialize_access(client, node="mng01", vmid=30000, name="pilot", password="temporary-password")

    assert [call[0] for call in client.exec_calls] == ["/usr/bin/cloud-init", "/bin/sh"]
    assert verified and verified[0][3] == "temporary-password"


def test_qga_timeout_is_fail_closed_and_never_dispatches_guest_exec(monkeypatch):
    client = QgaClient([ProxmoxError("POWER_PROXMOX_UNAVAILABLE", 500)] * 3)
    monkeypatch.setenv("IACP_GUEST_INIT_TIMEOUT", "0")

    with pytest.raises(RuntimeError, match="GUEST_AGENT_NOT_READY"):
        guest.initialize_access(client, node="mng01", vmid=30000, name="pilot", password="temporary-password")

    assert client.exec_calls == []
