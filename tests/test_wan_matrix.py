from scripts.wan_matrix import cases


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


def test_static_wan_binding_uses_vmbr3_untagged_for_upstream_vlan_211(tmp_path):
    from types import SimpleNamespace
    from scripts.wan_matrix import Matrix

    matrix = object.__new__(Matrix)
    matrix.profile = "prod_static"
    matrix.static_bridge = "vmbr3"
    matrix.static_vlan_tag = None
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
        assert "IACP_PROD_STATIC_PROXMOX_VLAN_TAG" in environment
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
    import pytest
    from types import SimpleNamespace
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
