import json
import os
import uuid
from pathlib import Path

import pytest

from api.db import database
from worker.allocation import allocate, release
from worker.main import write_backend
from worker.main import physical_presence
from worker.proxmox import ProxmoxError


class EmptyInventory:
    def inventory(self):
        return []


class FlakyInventory:
    def __init__(self, responses):
        self.responses = iter(responses)

    def inventory(self):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def seed_operation(operation_id: str, name: str) -> None:
    from datetime import datetime, timezone

    timestamp = datetime.now(timezone.utc).isoformat()
    with database() as db:
        db.execute(
            "INSERT INTO operations(operation_id, command, action, status, payload_json, instance_id, stack, created_at) VALUES (?, 'plan', 'provision', 'pending', '{}', ?, 'mng01', ?)",
            (operation_id, operation_id, timestamp),
        )
        db.execute(
            "INSERT INTO instances(instance_id, operation_id, name, image, status, spec_json, created_at, updated_at) VALUES (?, ?, ?, 'ubuntu-2404', 'pending', '{}', ?, ?)",
            (operation_id, operation_id, name, timestamp, timestamp),
        )


def test_allocation_is_postgres_safe_and_does_not_reuse_released_ids(monkeypatch):
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    minimum = 38000 + (int(first[:8], 16) % 500) * 3
    monkeypatch.setenv("IACP_VMID_MIN", str(minimum))
    monkeypatch.setenv("IACP_VMID_MAX", str(minimum + 2))
    seed_operation(first, f"allocation-{first[:8]}")
    seed_operation(second, f"allocation-{second[:8]}")
    client = EmptyInventory()
    assert allocate(first, client) == minimum
    release(minimum)
    assert allocate(second, client) == minimum + 1


def test_wasabi_backend_contains_instance_key_but_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("IACP_WASABI_BUCKET", "iacp-lab")
    monkeypatch.setenv("IACP_WASABI_REGION", "us-east-1")
    monkeypatch.setenv("IACP_WASABI_ENDPOINT", "https://s3.us-east-1.wasabisys.com")
    path = write_backend(str(tmp_path), "instance-123")
    contents = Path(path).read_text(encoding="utf-8")
    assert 'key = "iacp/lab/instances/instance-123/terraform.tfstate"' in contents
    assert "AWS_ACCESS_KEY" not in contents
    assert "AWS_SECRET" not in contents
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_physical_presence_retries_transient_read_only_proxmox_error(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_PRESENCE_CHECKS", "3")
    monkeypatch.setenv("IACP_PROXMOX_PRESENCE_CHECK_DELAY_SECONDS", "0")
    client = FlakyInventory([
        ProxmoxError("POWER_PROXMOX_UNAVAILABLE"),
        [{"vmid": 30004}],
    ])

    assert physical_presence(client, 30004, True) is True


def test_physical_presence_fails_closed_after_bounded_observation(monkeypatch):
    monkeypatch.setenv("IACP_PROXMOX_PRESENCE_CHECKS", "2")
    monkeypatch.setenv("IACP_PROXMOX_PRESENCE_CHECK_DELAY_SECONDS", "0")
    client = FlakyInventory([
        ProxmoxError("POWER_PROXMOX_UNAVAILABLE"),
        ProxmoxError("POWER_PROXMOX_UNAVAILABLE"),
    ])

    with pytest.raises(ProxmoxError, match="POWER_PROXMOX_UNAVAILABLE"):
        physical_presence(client, 30004, True)


def test_worker_policy_and_guest_contract_are_technical():
    compose = Path("compose.technical.yaml").read_text(encoding="utf-8")
    guest = Path("worker/guest.py").read_text(encoding="utf-8")
    catalog = json.loads(Path("terraform/environments/mng01/catalog.json").read_text(encoding="utf-8"))
    assert all(f"worker-{number}:" in compose for number in range(1, 7))
    assert "IACP_MAX_CONCURRENT" in compose
    assert "redis" not in compose.lower()
    assert [plan["plan_id"] for plan in catalog["plans"]] == ["kvm-1", "kvm-2", "kvm-3", "kvm-4"]
    assert [image["image_id"] for image in catalog["images"]] == [
        "ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10"
    ]
    assert "install -d -m 0755 /run/sshd" in guest
    assert "export PATH=/usr/sbin:/usr/bin:/sbin:/bin" in guest
    assert "/usr/sbin/chpasswd" in guest
