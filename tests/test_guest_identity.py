import ast
import hashlib
import json
from urllib.parse import parse_qsl

import pytest

from worker.guest_identity import (
    MODE,
    NETWORK_FILE,
    PROGRAM,
    PROGRAM_SHA256,
    GuestIdentityError,
    ensure,
    required,
)
from worker.proxmox import ProxmoxClient
import worker.main as worker_main


INSTANCE_ID = "00000000-0000-4000-8000-000000000001"
MAC = "bc:24:11:12:34:56"
IP = "192.0.2.10"
MACHINE_ID = "8fa379f7f6414dfe8ec047307f8af2d2\n"
NETWORK_CONFIG = '{"network":{"version":2,"ethernets":{"ens18":{"dhcp-identifier":"mac"}}}}\n'


class FakeProxmox:
    def __init__(self, *, grants=None, client_id="01bc:24:11:12:34:56"):
        self.grants = grants or {"VM.GuestAgent.Unrestricted": 1}
        self.client_id = client_id
        self.exec_calls = []

    def vm_config(self, node, vmid):
        return {
            "name": "kvm-unique-ip",
            "template": 0,
            "net0": f"virtio={MAC},bridge=vmbr0",
            "ipconfig0": "ip=dhcp",
        }

    def get(self, path):
        return {"/vms/30000": self.grants}

    def guest_agent_info(self, node, vmid):
        return {"version": "test"}

    def guest_agent_exec(self, node, vmid, command):
        self.exec_calls.append((node, vmid, command))
        return {"pid": 41}

    def guest_agent_exec_status(self, node, vmid, pid):
        return {"exited": True, "exitcode": 0}

    def guest_agent_file_read(self, node, vmid, path):
        marker = {
            "instance_id": INSTANCE_ID,
            "mac_address": MAC,
            "mode": MODE,
            "program_sha256": PROGRAM_SHA256,
            "machine_id_sha256": hashlib.sha256(MACHINE_ID.encode()).hexdigest(),
            "network_sha256": hashlib.sha256(NETWORK_CONFIG.encode()).hexdigest(),
            "ifindex": 2,
        }
        files = {
            "/var/lib/iacp/guest-identity.json": json.dumps(marker),
            "/etc/machine-id": MACHINE_ID,
            NETWORK_FILE: NETWORK_CONFIG,
            "/run/systemd/netif/leases/2": f"ADDRESS={IP}\nCLIENTID={self.client_id}\n",
        }
        if path not in files:
            from worker.proxmox import ProxmoxError

            raise ProxmoxError("POWER_PROXMOX_UNAVAILABLE", 500)
        return {"content": files[path]}

    def guest_agent_interfaces(self, node, vmid):
        return [
            {
                "name": "ens18",
                "hardware-address": MAC,
                "ip-addresses": [{"ip-address": IP, "ip-address-type": "ipv4"}],
            }
        ]


def test_identity_is_scoped_to_ubuntu_2404_dhcp_on_mng():
    spec = {"image": "ubuntu-2404", "network_profile": "lab_dhcp", "network_mode": "dhcp"}
    assert required(spec, "mng01", 30000)
    assert required(spec, "mng01", 49999)
    assert not required(spec, "prx", 30000)
    assert not required(spec, "mng01", 29000)
    assert not required({**spec, "image": "ubuntu-2604"}, "mng01", 30000)
    assert not required({**spec, "network_mode": "static"}, "mng01", 30000)


def test_guest_identity_initializes_once_and_waits_for_mac_bound_lease():
    client = FakeProxmox()
    result = ensure(
        client,
        node="mng01",
        vmid=30000,
        name="kvm-unique-ip",
        instance_id=INSTANCE_ID,
        timeout=1,
        poll_interval=0.001,
    )
    assert result == {"mac_address": MAC, "ipv4": IP, "mode": MODE}
    assert len(client.exec_calls) == 1
    command = client.exec_calls[0][2]
    assert command[:3] == ["/usr/bin/python3", "-c", PROGRAM]
    assert command[-2:] == [MAC, PROGRAM_SHA256]


def test_guest_identity_fails_closed_without_guest_agent_permission():
    client = FakeProxmox(grants={"VM.GuestAgent.Unrestricted": 0})
    with pytest.raises(GuestIdentityError, match="GUEST_IDENTITY_PERMISSION_REQUIRED"):
        ensure(
            client,
            node="mng01",
            vmid=30000,
            name="kvm-unique-ip",
            instance_id=INSTANCE_ID,
            timeout=0.1,
            poll_interval=0.001,
        )
    assert client.exec_calls == []


def test_guest_identity_does_not_accept_lease_claimed_by_another_dhcp_client():
    client = FakeProxmox(client_id="ff:duplicate-duid")
    with pytest.raises(GuestIdentityError, match="GUEST_IDENTITY_OBSERVATION_TIMEOUT"):
        ensure(
            client,
            node="mng01",
            vmid=30000,
            name="kvm-unique-ip",
            instance_id=INSTANCE_ID,
            timeout=0.01,
            poll_interval=0.001,
        )


def test_guest_program_is_one_shot_and_changes_machine_and_dhcp_identity():
    ast.parse(PROGRAM)
    assert "flock(stream,fcntl.LOCK_EX)" in PROGRAM
    assert "uuid.uuid4().hex" in PROGRAM
    assert '"dhcp-identifier":"mac"' in PROGRAM.replace("'", '"')
    assert "pending.unlink(missing_ok=True)" in PROGRAM
    assert r"write(marker,json.dumps(evidence,sort_keys=True)+'\n')" in PROGRAM


def test_worker_only_dispatches_identity_for_running_supported_guest(monkeypatch):
    events = []
    calls = []
    monkeypatch.setattr(worker_main, "emit", lambda *args: events.append(args))
    monkeypatch.setattr(worker_main, "heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_main, "ensure_guest_identity", lambda client, **kwargs: calls.append(kwargs) or {"ipv4": IP})
    spec = {"image": "ubuntu-2404", "network_profile": "lab_dhcp", "network_mode": "dhcp"}
    common = {
        "worker_id": "worker-1",
        "operation_id": "operation-1",
        "client": object(),
        "stack": "mng01",
        "node": "mng01",
        "vmid": 30000,
        "name": "kvm-unique-ip",
        "instance_id": INSTANCE_ID,
        "spec": spec,
    }
    assert worker_main.ensure_guest_identity_for_operation(**common, started=False) is None
    assert calls == []
    result = worker_main.ensure_guest_identity_for_operation(**common, started=True)
    assert result == {"ipv4": IP}
    assert len(calls) == 1 and calls[0]["instance_id"] == INSTANCE_ID
    assert events[0][1:3] == ("guest.identity.deferred", "pending")
    assert events[-1][1:3] == ("guest.identity.verified", "succeeded")


def test_proxmox_guest_exec_encodes_repeated_command_parameters(monkeypatch):
    client = ProxmoxClient(endpoint="https://pve.invalid", token="not-used")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"data":{"pid":7}}'

    def open_request(request, timeout):
        captured["request"] = request
        return Response()

    monkeypatch.setattr(client.opener, "open", open_request)
    command = ["/usr/bin/python3", "-c", "print('ok')", INSTANCE_ID, MAC, PROGRAM_SHA256]
    assert client.guest_agent_exec("mng01", 30000, command) == {"pid": 7}
    assert [value for key, value in parse_qsl(captured["request"].data.decode()) if key == "command"] == command
