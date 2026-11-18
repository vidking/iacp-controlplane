import pytest

pytestmark = pytest.mark.no_database

from scripts.wan_matrix import (
    cases,
    require_four_idle_service_workers,
    verify_netbox_state,
    wan_nic_config,
)


def test_wan_matrices_cover_exact_combinations_and_six_item_batches():
    for category in ['kvm','services','agents']:
        matrix=cases(category)
        assert len(matrix)==18 and len(matrix)%6==0
        main=[case for case in matrix if not case['control']]
        assert len(main)==16
        assert len({(case['product'],case['image'],case['plan']) for case in main})==16
        if category!='kvm':assert {case['image'] for case in main}=={'ubuntu-2404','almalinux-9'}


def test_service_controls_cover_password_only_access():
    matrix = cases('services')
    controls = [case for case in matrix if case['control']]
    assert len(controls) == 2
    assert {case['product'] for case in controls} == {'nextcloud', 'n8n'}
    assert all(case['client_ssh_key'] is False for case in controls)


def test_wan_nic_preflight_allows_only_vmbr3():
    import pytest

    assert wan_nic_config({"IACP_PROD_STATIC_BRIDGE": "vmbr3"}) == "vmbr3"
    with pytest.raises(RuntimeError, match="WAN_BRIDGE_NOT_AUTHORIZED"):
        wan_nic_config({"IACP_PROD_STATIC_BRIDGE": "vmbr0"})


def test_wan_preflight_requires_exactly_four_fresh_idle_service_workers():
    from datetime import UTC, datetime

    import pytest

    heartbeat = datetime.now(UTC).isoformat()
    state = {
        "max_concurrent": 4,
        "workers": [
            {"worker_id": f"service-worker-{index}", "status": "idle", "job_id": None, "last_heartbeat": heartbeat}
            for index in range(1, 5)
        ],
    }
    require_four_idle_service_workers(state)
    with pytest.raises(RuntimeError, match="FOUR_SERVICE_WORKERS_REQUIRED"):
        require_four_idle_service_workers({"max_concurrent": 2, "workers": state["workers"][:2]})
    busy = {**state, "workers": [dict(worker) for worker in state["workers"]]}
    busy["workers"][0]["status"] = "running"
    busy["workers"][0]["job_id"] = "job-active"
    with pytest.raises(RuntimeError, match="SERVICE_WORKERS_NOT_IDLE"):
        require_four_idle_service_workers(busy)


def test_wan_preflight_uses_only_current_almalinux_goldens():
    import pytest

    from scripts.wan_matrix import require_expected_golden_images

    images = [
        {"image_id": image_id, "template_vm_id": vmid}
        for image_id, vmid in (("almalinux-9", 29010), ("almalinux-10", 29011))
    ]
    inventory = [
        {"vmid": vmid, "node": "mng01"}
        for vmid in (29010, 29011)
    ]
    assert require_expected_golden_images(
        images, inventory, lambda _node, _vmid: {"template": 1}
    ) == images

    obsolete = [
        {"image_id": image_id, "template_vm_id": vmid}
        for image_id, vmid in (("almalinux-9", 29016), ("almalinux-10", 29017))
    ]
    with pytest.raises(RuntimeError, match="GOLDEN_IMAGE_CATALOG_MISMATCH"):
        require_expected_golden_images(
            obsolete, inventory, lambda _node, _vmid: {"template": 1}
        )

    with pytest.raises(RuntimeError, match="GOLDEN_IMAGE_NOT_READY"):
        require_expected_golden_images(
            images, inventory[:1], lambda _node, _vmid: {"template": 1}
        )


def test_wan_preflight_checks_authorized_netbox_prefix_and_preserves_reserved_ip():
    from ipaddress import IPv4Address, IPv4Network
    from types import SimpleNamespace

    class NetBoxStub:
        config = SimpleNamespace(prefix_id=52)

        def get_prefix(self):
            return IPv4Network("190.0.231.0/24"), {}

        def validate_gateway(self, _prefix):
            return "190.0.231.1"

        def get_ip(self, address):
            assert address == "190.0.231.2/24"
            return {"status": {"value": "reserved"}, "tags": []}

        def available_addresses(self, _prefix):
            return [(IPv4Address(f"190.0.231.{host}"), 24) for host in range(3, 9)]

    assert verify_netbox_state(NetBoxStub()) == {
        "prefix_id": 52,
        "prefix": "190.0.231.0/24",
        "gateway": "190.0.231.1",
        "reserved_address": "190.0.231.2/24",
        "next_available": "190.0.231.3/24",
    }

    class UnreservedNetBox(NetBoxStub):
        def get_ip(self, _address):
            return {"status": {"value": "active"}, "tags": []}

    with pytest.raises(RuntimeError, match="NETBOX_RESERVED_ADDRESS_INVALID"):
        verify_netbox_state(UnreservedNetBox())

    class OutOfScopeNetBox(NetBoxStub):
        def available_addresses(self, _prefix):
            return [(IPv4Address("190.0.231.254"), 24)]

    with pytest.raises(RuntimeError, match="NETBOX_AUTHORIZED_RANGE_EXHAUSTED"):
        verify_netbox_state(OutOfScopeNetBox())


def test_static_wan_binding_uses_vmbr3_without_proxmox_tag(tmp_path):
    from types import SimpleNamespace

    from scripts.wan_matrix import Matrix

    matrix = object.__new__(Matrix)
    matrix.profile = "prod_static"
    matrix.static_bridge = "vmbr3"
    matrix.folder = tmp_path
    matrix.proxmox = SimpleNamespace(
        vm_config=lambda *_: {
            "name": "prueba-service-nextcloud-c1-01",
            "net0": "virtio=aa:bb:cc:dd:ee:ff,bridge=vmbr3",
            "ipconfig0": "ip=190.0.231.8/24,gw=190.0.231.1",
        }
    )
    item = {
        "vmid": 40008,
        "name": "prueba-service-nextcloud-c1-01",
        "instance_id": "instance-test",
        "image": "ubuntu-2404",
        "address": "190.0.231.8",
    }
    matrix.qga = lambda *_: "ssh-ed25519 AAAAexample"
    matrix.binding(item)
    assert (tmp_path / "known-40008").read_text() == (
        "190.0.231.8 ssh-ed25519 AAAAexample\n"
    )


def test_compose_passes_wan_nic_settings_to_service_workers_without_proxmox_tokens():
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).parents[1] / "deploy/portainer/docker-compose.yml").read_text()
    )
    for worker in ("service-worker-1", "service-worker-2", "service-worker-3", "service-worker-4"):
        environment = compose["services"][worker]["environment"]
        assert "IACP_PROD_STATIC_BRIDGE" in environment
        assert "IACP_PROXMOX_MNG_API_TOKEN" not in environment
        assert "IACP_PROXMOX_API_TOKEN" not in environment


def test_parallel_failure_contains_future_mutations():
    import threading

    import pytest

    from scripts.wan_matrix import Matrix
    matrix=object.__new__(Matrix)
    matrix.abort=threading.Event()
    def fail(_):raise RuntimeError('FALLO_CONTROLADO')
    with pytest.raises(RuntimeError):matrix.parallel(fail,range(6))
    assert matrix.abort.is_set()


def test_record_keeps_matrix_running_when_ssh_output_closes(tmp_path, monkeypatch):
    import json
    import sys

    from scripts.wan_matrix import Matrix

    class ClosedOutput:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    matrix = object.__new__(Matrix)
    matrix.log = tmp_path / 'results.jsonl'
    monkeypatch.setattr(sys, 'stdout', ClosedOutput())
    matrix.record('verified', name='prueba-service-n8n-c1-01')
    assert json.loads(matrix.log.read_text())['event'] == 'verified'


def test_almalinux_matrix_pins_key_through_validated_helper(tmp_path):
    import json
    from types import SimpleNamespace

    import pytest

    from scripts.wan_matrix import Matrix
    matrix = object.__new__(Matrix)
    matrix.profile = 'lab_dhcp'
    matrix.folder = tmp_path
    matrix.proxmox = SimpleNamespace(vm_config=lambda *_: {
        'name': 'test', 'net0': 'e1000=aa:bb:cc:dd:ee:ff,bridge=vmbr0', 'ipconfig0': 'ip=dhcp',
    })
    item = {'vmid': 40000, 'name': 'test', 'instance_id': 'instance-test', 'image': 'almalinux-9', 'address': '192.0.2.10'}
    observed = {'bootstrap_protocol': 1, 'instance_id': 'instance-test', 'mac_address': 'aa:bb:cc:dd:ee:ff', 'host_key': 'ssh-ed25519 AAAAexample'}
    commands = []
    matrix.qga = lambda _vmid, argv: commands.append(argv) or json.dumps(observed)
    matrix.binding(item)
    assert commands[0] == ['/usr/local/libexec/iacp-guest-bootstrap', 'instance-test', 'aa:bb:cc:dd:ee:ff', 'dhcp', '', '']
    assert (tmp_path/'known-40000').read_text() == '192.0.2.10 ssh-ed25519 AAAAexample\n'
    observed['instance_id'] = 'another-instance'
    with pytest.raises(AssertionError):
        matrix.binding(item)


def test_wait_service_retries_known_startup_failure_after_worker_release(monkeypatch):
    import scripts.wan_matrix as wan_matrix

    matrix = object.__new__(wan_matrix.Matrix)
    job_id = "job-test"
    states = iter(
        [
            {
                "job_id": job_id,
                "status": "uncertain",
                "error": "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_FAILED_APPLICATION_START",
                "worker_id": "service-worker-4",
            },
            {"job_id": job_id, "status": "succeeded", "error": ""},
        ]
    )
    calls = []

    def api(method, path, body=None, key=None):
        calls.append((method, path, body, key))
        if path == f"/v1/service-jobs/{job_id}":
            return next(states)
        if path == "/v1/service-workers":
            return {
                "workers": [
                    {
                        "worker_id": "service-worker-4",
                        "status": "running",
                        "job_id": "another-job",
                    }
                ]
            }
        if path == f"/v1/service-jobs/{job_id}/retry":
            return {"job_id": job_id, "status": "queued"}
        raise AssertionError(path)

    matrix.api = api
    matrix.record = lambda *_args, **_kwargs: None
    monkeypatch.setattr(wan_matrix.time, "sleep", lambda _seconds: None)
    matrix.wait_service(job_id)
    assert calls[2] == (
        "POST",
        f"/v1/service-jobs/{job_id}/retry",
        {"previous_execution_stopped": True},
        f"matrix-service-retry-{job_id}",
    )


def test_wait_service_does_not_retry_when_original_worker_still_owns_job(monkeypatch):
    import pytest

    import scripts.wan_matrix as wan_matrix

    matrix = object.__new__(wan_matrix.Matrix)
    job_id = "job-test"
    calls = []

    def api(method, path, body=None, key=None):
        calls.append((method, path))
        if path == f"/v1/service-jobs/{job_id}":
            return {
                "job_id": job_id,
                "status": "uncertain",
                "error": "SERVICE_EXECUTION_UNCERTAIN:ANSIBLE_FAILED_APPLICATION_START",
                "worker_id": "service-worker-4",
            }
        if path == "/v1/service-workers":
            return {
                "workers": [
                    {
                        "worker_id": "service-worker-4",
                        "status": "running",
                        "job_id": job_id,
                    }
                ]
            }
        raise AssertionError("must not retry an active execution")

    matrix.api = api
    matrix.record = lambda *_args, **_kwargs: None
    monkeypatch.setattr(wan_matrix.time, "sleep", lambda _seconds: None)
    with pytest.raises(RuntimeError, match="SERVICE_PREVIOUS_EXECUTION_NOT_STOPPED"):
        matrix.wait_service(job_id)
    assert not any(path.endswith("/retry") for _, path in calls)
