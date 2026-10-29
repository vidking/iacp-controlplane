"""Regresiones del contrato entre Cloud-Init y acceso SSH root."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import paramiko
import pytest

from iacp.lifecycle.proxmox import Target
from iacp.services.access import AccessVerifier
from iacp.services.instances import Conflict


class FakeHostKeys:
    def add(self, address, key_type, key):
        self.value = (address, key_type, key)


class FakeStdout:
    class Channel:
        def recv_exit_status(self):
            return 0

    channel = Channel()

    def read(self, _count):
        return b"0\n"


class FakeSSH:
    def __init__(self, *, failure=None):
        self.failure = failure
        self.username = None
        self.password = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_host_keys(self):
        return FakeHostKeys()

    def set_missing_host_key_policy(self, _policy):
        return None

    def connect(self, address, username, password, **_kwargs):
        self.username = username
        self.password = password
        if self.failure is not None:
            raise self.failure

    def exec_command(self, _command, timeout):
        assert timeout == 5
        return None, FakeStdout(), None


class FakeQgaClient:
    def __init__(self, name):
        self.name = name

    def observe(self, _target, *, preflight):
        assert preflight is True
        return {"power_state": "running"}

    def _request(self, method, path):
        assert method == "GET"
        if path.endswith("/config?current=1"):
            return {"name": self.name, "ciuser": "ubuntu"}
        assert "file-read" in path
        file_name = parse_qs(urlsplit(path).query)["file"][0]
        values = {
            "/var/lib/cloud/instance/boot-finished": "finished\n",
            "/etc/passwd": "root:x:0:0:root:/root:/bin/bash\nubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n",
            "/var/lib/cloud/data/result.json": json.dumps({"v1": {"errors": []}}),
        }
        return {"content": values[file_name], "truncated": False}


@pytest.fixture
def verifier_target(monkeypatch):
    instance = SimpleNamespace(name="prueba-kvm-2404-003", image="ubuntu-2404",
                               ip_addresses=["192.0.2.10"])
    target = Target(30101, "mng01", instance.name)
    qga = FakeQgaClient(instance.name)
    ssh = FakeSSH()
    monkeypatch.setattr(
        "iacp.services.guest_identity.GuestClient.configured",
        classmethod(lambda cls: SimpleNamespace(client=qga)),
    )
    monkeypatch.setattr("iacp.services.ssh_host_identity.read",
                        lambda _client, _target, _image: SimpleNamespace(key=b"host-key"))
    monkeypatch.setattr(paramiko, "SSHClient", lambda: ssh)
    return instance, target, ssh


def test_ciuser_ubuntu_allows_real_root_ssh(verifier_target):
    instance, target, ssh = verifier_target

    result = AccessVerifier().verify(
        target, instance, password="SENUELO-TECNICO-VALIDO-2026",
        ip_addresses=["192.0.2.10"],
    )

    assert result["cloud_init_complete"] is True
    assert result["ssh_authenticated"] is True
    assert ssh.username == "root"
    assert ssh.password == "SENUELO-TECNICO-VALIDO-2026"


def test_wrong_root_password_is_not_ready(verifier_target):
    instance, target, ssh = verifier_target
    ssh.failure = paramiko.AuthenticationException()

    with pytest.raises(paramiko.AuthenticationException):
        AccessVerifier().verify(
            target, instance, password="SENUELO-TECNICO-INCORRECTO-2026",
            ip_addresses=["192.0.2.10"],
        )


def test_missing_root_account_fails_closed(verifier_target, monkeypatch):
    instance, target, _ssh = verifier_target

    original = FakeQgaClient._request

    def no_root(self, method, path):
        response = original(self, method, path)
        if parse_qs(urlsplit(path).query).get("file") == ["/etc/passwd"]:
            response["content"] = "ubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n"
        return response

    monkeypatch.setattr(FakeQgaClient, "_request", no_root)
    with pytest.raises(Conflict, match="ROOT_ACCESS_EVIDENCE_INVALID"):
        AccessVerifier().verify(
            target, instance, password="SENUELO-TECNICO-VALIDO-2026",
            ip_addresses=["192.0.2.10"],
        )
