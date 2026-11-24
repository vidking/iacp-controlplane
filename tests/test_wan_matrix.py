import pytest

pytestmark = pytest.mark.no_database

from scripts.wan_matrix import (
    Matrix,
    application_certification_matrix,
    allocate_matrix_names,
    cases,
    credential_delivery_headers,
    kvm_certification_matrix,
    matrix_capacity_batch,
    matrix_netbox_tags,
    matrix_runtime,
    nextcloud_request_token,
    production_matrix_category_allowed,
    production_service_batches,
    production_service_matrix,
    production_service_pilot_indexes,
    require_kvm_capacity,
    require_six_idle_service_workers,
    required_cycles,
    ssh_failure_category,
    verify_netbox_state,
    wan_nic_config,
)
from scripts import wan_matrix


def test_wan_matrices_cover_exact_combinations_and_six_item_batches():
    matrix=cases('agents')
    assert len(matrix)==18 and len(matrix)%6==0
    main=[case for case in matrix if not case['control']]
    assert len(main)==16
    assert len({(case['product'],case['image'],case['plan']) for case in main})==16


def test_kvm_wan_matrix_covers_all_six_images_and_four_plans():
    matrix = cases("kvm")
    main = [case for case in matrix if not case["control"]]
    assert len(main) == 24 and len(matrix) == 26
    assert {(case["image"], case["plan"]) for case in main} == {
        (image, f"kvm-{plan}")
        for image in ("ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10", "fedora-45", "debian-13")
        for plan in range(1, 5)
    }


def test_kvm_certification_matrix_covers_all_image_plan_access_combinations():
    matrix = kvm_certification_matrix()
    assert len(matrix) == 48
    assert len({(case["image"], case["plan"], case["access_mode"]) for case in matrix}) == 48
    assert {case["access_mode"] for case in matrix} == {"ssh_key", "ssh_password"}
    for image in ("ubuntu-2404", "ubuntu-2604", "almalinux-9", "almalinux-10", "fedora-45", "debian-13"):
        for plan in range(1, 5):
            assert sum(case["image"] == image and case["plan"] == f"kvm-{plan}" for case in matrix) == 2


def test_application_certification_matrix_covers_12_ssh_and_12_gui_cases():
    matrix = application_certification_matrix()
    infrastructure = [case for case in matrix if case["certification_track"] == "infrastructure_access"]
    gui = [case for case in matrix if case["certification_track"] == "gui_persistence"]
    assert len(matrix) == 24 and len(infrastructure) == len(gui) == 12
    assert sum(case["access_mode"] == "ssh_key" for case in infrastructure) == 6
    assert sum(case["access_mode"] == "ssh_password" for case in infrastructure) == 6
    assert sum(case["product"] == "n8n" for case in gui) == 6
    assert sum(case["product"] == "nextcloud" for case in gui) == 6
    assert {case["plan"] for case in matrix} == {f"kvm-{plan}" for plan in range(1, 5)}
    assert all(case["image"] == "ubuntu-2404" for case in matrix)


def test_application_certification_matrix_rejects_non_ubuntu_image():
    with pytest.raises(ValueError, match="APPLICATION_MATRIX_REQUIRES_UBUNTU_2404"):
        application_certification_matrix(["almalinux-9"])


def test_matrix_sends_credential_receipt_and_sanitizes_delivery_errors(monkeypatch):
    import io
    import threading
    from email.message import Message
    from urllib.error import HTTPError

    receipt = "r" * 43
    delivery_headers = credential_delivery_headers({"credential_receipt": receipt})
    assert delivery_headers == {"X-IACP-Credential-Receipt": receipt}
    with pytest.raises(RuntimeError, match="SERVICE_CREDENTIAL_RECEIPT_MISSING"):
        credential_delivery_headers({})

    class Response(io.BytesIO):
        def __init__(self, body):
            super().__init__(body)
            self.headers = Message()
            self.headers["X-IACP-Build-Revision"] = "revision-under-test"

    captured = {}

    def successful_urlopen(request, timeout):
        captured["receipt"] = next(
            value
            for name, value in request.header_items()
            if name.lower() == "x-iacp-credential-receipt"
        )
        return Response(b'{"job_id":"job-1","status":"queued"}')

    monkeypatch.setattr(wan_matrix, "urlopen", successful_urlopen)
    matrix = Matrix.__new__(Matrix)
    matrix.token = "test-token"
    matrix.base = "https://dev-iac.example.invalid"
    matrix.env = {"IACP_IMAGE_TAG": "revision-under-test"}
    matrix.abort = threading.Event()
    matrix.api_lock = threading.Lock()
    matrix.last_request = 0.0
    response = matrix.api(
        "POST",
        "/v1/service-instances/service-1/deliver",
        key="delivery-test",
        extra_headers=delivery_headers,
    )
    assert response == {"job_id": "job-1", "status": "queued"}
    assert captured["receipt"] == receipt

    def conflict_urlopen(request, timeout):
        raise HTTPError(
            request.full_url,
            409,
            "conflict",
            Message(),
            io.BytesIO(b'{"detail":"SERVICE_CREDENTIAL_RECEIPT_REQUIRED"}'),
        )

    monkeypatch.setattr(wan_matrix, "urlopen", conflict_urlopen)
    with pytest.raises(RuntimeError, match="API_409_POST_SERVICE-INSTANCES"):
        matrix.api(
            "POST",
            "/v1/service-instances/service-1/deliver",
            key="delivery-test",
            extra_headers=delivery_headers,
        )


def test_service_default_matrix_is_eight_n8n_and_eight_nextcloud_on_ubuntu_2404():
    for cycle in (1, 2):
        matrix = cases("services", cycle=cycle)
        main = [case for case in matrix if not case["control"]]
        assert len(main) == 16 and len(matrix) == 18
        assert {case["image"] for case in matrix} == {"ubuntu-2404"}
        assert sum(case["product"] == "n8n" for case in main) == 8
        assert sum(case["product"] == "nextcloud" for case in main) == 8


def test_service_matrix_can_run_ubuntu_only_without_waiting_for_almalinux():
    matrix = cases("services", images=["ubuntu-2404"])
    main = [case for case in matrix if not case["control"]]
    assert len(matrix) == 18 and len(main) == 16
    assert {case["image"] for case in main} == {"ubuntu-2404"}
    assert {(case["product"], case["plan"]) for case in main} == {
        (product, f"kvm-{plan}")
        for product in ("nextcloud", "n8n")
        for plan in range(1, 5)
    }
    assert all(
        sum(
            case["product"] == product and case["plan"] == plan
            for case in main
        )
        == 2
        for product in ("nextcloud", "n8n")
        for plan in (f"kvm-{n}" for n in range(1, 5))
    )


def test_service_certification_exact_cases_are_eight_unique_ubuntu_services():
    matrix = cases("services", images=["ubuntu-2404"], include_controls=False)
    assert len(matrix) == 8
    assert all(case["image"] == "ubuntu-2404" and not case["control"] for case in matrix)
    assert [(case["product"], case["plan"]) for case in matrix] == [
        (product, f"kvm-{plan}")
        for product in ("n8n", "nextcloud")
        for plan in range(1, 5)
    ]


def test_production_full_service_matrix_keeps_all_16_cases_and_both_controls():
    matrix = production_service_matrix(["ubuntu-2404"], full_matrix=True)
    main = [case for case in matrix if not case["control"]]
    controls = [case for case in matrix if case["control"]]
    assert len(main) == 16 and len(controls) == 2
    assert sum(case["product"] == "n8n" for case in main) == 8
    assert sum(case["product"] == "nextcloud" for case in main) == 8
    assert all(case["image"] == "ubuntu-2404" for case in matrix)
    assert {case["product"] for case in controls} == {"n8n", "nextcloud"}
    assert all(case["client_ssh_key"] is False for case in controls)
    pilot_indexes = production_service_pilot_indexes(matrix)
    assert len({matrix[index]["product"] for index in pilot_indexes}) == 2
    batches = production_service_batches(matrix)
    assert [len(batch) for batch in batches] == [1, 1, 6, 6, 4]
    covered_indexes = sorted(index for batch in batches for index, _case in batch)
    assert covered_indexes == list(range(18))
    assert sum(bool(case.get("pilot")) for batch in batches for _, case in batch) == 2


def test_production_scoped_service_matrix_remains_eight_cases_without_controls():
    matrix = production_service_matrix(["ubuntu-2404"], full_matrix=False)
    assert len(matrix) == 8
    assert not any(case["control"] for case in matrix)
    pilot_indexes = production_service_pilot_indexes(matrix)
    assert len({matrix[index]["product"] for index in pilot_indexes}) == 2


def test_production_service_matrix_rejects_other_images():
    with pytest.raises(ValueError, match="PRODUCTION_MATRIX_REQUIRES_UBUNTU_2404"):
        production_service_matrix(["almalinux-9"], full_matrix=True)


def test_production_kvm_scope_requires_explicit_full_matrix_authorization():
    assert production_matrix_category_allowed("services", preflight_only=False)
    assert production_matrix_category_allowed("kvm", preflight_only=True)
    assert not production_matrix_category_allowed("kvm", preflight_only=False)
    assert production_matrix_category_allowed(
        "kvm", preflight_only=False, full_kvm_matrix=True
    )
    assert not production_matrix_category_allowed("agents", preflight_only=True)
    assert not production_matrix_category_allowed(
        "agents", preflight_only=False, full_kvm_matrix=True
    )


def test_kvm_capacity_preflight_uses_the_selected_six_image_catalog():
    images = [
        "ubuntu-2404",
        "ubuntu-2604",
        "almalinux-9",
        "almalinux-10",
        "fedora-45",
        "debian-13",
    ]
    batch = matrix_capacity_batch("kvm", images, cycle=1)
    assert len(batch) == 6
    assert {(case["image"], case["plan"]) for case in batch} == {
        ("ubuntu-2404", "kvm-1"),
        ("ubuntu-2404", "kvm-2"),
        ("ubuntu-2404", "kvm-3"),
        ("ubuntu-2404", "kvm-4"),
        ("ubuntu-2604", "kvm-1"),
        ("ubuntu-2604", "kvm-2"),
    }


def test_fedora_kvm_matrix_repeats_four_plans_across_six_worker_batches():
    matrix = cases("kvm", images=["fedora-45"])
    assert len(matrix) == 18
    cases_only = [case for case in matrix if not case["control"]]
    assert len(cases_only) == 16
    assert {case["image"] for case in matrix} == {"fedora-45"}
    assert {case["plan"] for case in cases_only} == {
        "kvm-1",
        "kvm-2",
        "kvm-3",
        "kvm-4",
    }
    assert all(sum(case["plan"] == f"kvm-{plan}" for case in cases_only) == 4 for plan in range(1, 5))
    assert sum(case["control"] for case in matrix) == 2
    assert [len(matrix[index : index + 6]) for index in range(0, len(matrix), 6)] == [6, 6, 6]


@pytest.mark.parametrize("image", ["fedora-45", "debian-13"])
def test_exact_kvm_matrix_runs_six_at_a_time_and_covers_all_four_plans(image):
    matrix = cases("kvm", images=[image], include_controls=False)
    assert len(matrix) == 16
    assert {case["image"] for case in matrix} == {image}
    assert {case["plan"] for case in matrix} == {
        "kvm-1",
        "kvm-2",
        "kvm-3",
        "kvm-4",
    }
    assert all(
        sum(case["plan"] == f"kvm-{plan}" for case in matrix) == 4
        for plan in range(1, 5)
    )
    assert [len(matrix[index : index + 6]) for index in range(0, len(matrix), 6)] == [6, 6, 4]
    assert not any(case["control"] for case in matrix)
    plan_memory = {"kvm-1": 2, "kvm-2": 4, "kvm-3": 8, "kvm-4": 16}
    assert [
        sum(plan_memory[case["plan"]] for case in matrix[index : index + 6])
        for index in (0, 6, 12)
    ] == [44, 44, 32]


def test_kvm_capacity_requires_six_worker_memory_and_storage_reserve():
    capacity = require_kvm_capacity(
        [{"node": "mng01", "status": "online", "mem": 20 * 1024**3, "maxmem": 100 * 1024**3}],
        {"avail": 2000 * 1024**3},
    )
    assert capacity["required_memory_gib"] == 48
    assert capacity["required_storage_gib"] == 1200

    with pytest.raises(RuntimeError, match="KVM_MEMORY_CAPACITY_INSUFFICIENT"):
        require_kvm_capacity(
            [{"node": "mng01", "status": "online", "mem": 60 * 1024**3, "maxmem": 100 * 1024**3}],
            {"avail": 2000 * 1024**3},
        )
    with pytest.raises(RuntimeError, match="KVM_STORAGE_CAPACITY_INSUFFICIENT"):
        require_kvm_capacity(
            [{"node": "mng01", "status": "online", "mem": 20 * 1024**3, "maxmem": 100 * 1024**3}],
            {"avail": 1000 * 1024**3},
        )


def test_nextcloud_request_token_parser_handles_hidden_input_and_data_attribute():
    token, shape = nextcloud_request_token(
        '<form><input value="temporary" type="hidden" name="requesttoken"></form>'
    )
    assert token == "temporary"
    assert shape["forms"] == 1
    assert shape["input_names"] == ["requesttoken"]

    token, shape = nextcloud_request_token(
        '<body data-requesttoken="temporary-body-token"><form></form></body>'
    )
    assert token == "temporary-body-token"
    assert shape["request_token_attributes"] == ["data-requesttoken"]


def test_ssh_failure_category_keeps_transport_and_guest_errors_distinct():
    assert ssh_failure_category(255, "Connection timed out") == "transport_timeout"
    assert ssh_failure_category(255, "Permission denied") == "authentication_failed"
    assert ssh_failure_category(1, "remote program failed") == "remote_command_error"


def test_matrix_waits_for_ssh_after_power_on_until_key_and_password_work(monkeypatch):
    connection_attempts = 0
    key_attempts = 0
    password_attempts = 0
    sleeps = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def connect(_address, timeout):
        nonlocal connection_attempts
        assert timeout == 5
        connection_attempts += 1
        if connection_attempts == 1:
            raise OSError("connection refused")
        return Connection()

    def ssh(_item, _argv, password=None, timeout=240):
        nonlocal key_attempts, password_attempts
        assert timeout == 20
        if password is None:
            key_attempts += 1
            if key_attempts == 1:
                raise RuntimeError("SSH_TRANSPORT_TIMEOUT_HOSTNAME_255")
        else:
            password_attempts += 1
        return "matrix-test-vm\n"

    monkeypatch.setattr("scripts.wan_matrix.socket.create_connection", connect)
    monkeypatch.setattr("scripts.wan_matrix.time.sleep", sleeps.append)
    matrix = Matrix.__new__(Matrix)
    matrix.ssh = ssh
    item = {
        "address": "190.0.231.10",
        "name": "matrix-test-vm",
        "password": "ephemeral-test-password",
    }

    matrix.wait_for_ssh_after_power_on(item, timeout=20)

    assert connection_attempts == 3
    assert key_attempts == 2
    assert password_attempts == 1
    assert sleeps == [2, 2]


def test_matrix_does_not_retry_ssh_authentication_failure(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "scripts.wan_matrix.socket.create_connection",
        lambda *_args, **_kwargs: Connection(),
    )
    matrix = Matrix.__new__(Matrix)
    matrix.ssh = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("SSH_AUTHENTICATION_FAILED")
    )
    item = {
        "address": "190.0.231.10",
        "name": "matrix-test-vm",
        "password": "ephemeral-test-password",
    }

    with pytest.raises(RuntimeError, match="SSH_AUTHENTICATION_FAILED"):
        matrix.wait_for_ssh_after_power_on(item, timeout=20)


def test_matrix_records_ssh_subprocess_timeout_as_safe_transport_failure(monkeypatch):
    import subprocess
    import threading

    events = []
    matrix = Matrix.__new__(Matrix)
    matrix.abort = threading.Event()
    matrix.askpass = "/tmp/unused-askpass"
    matrix.record = lambda event, **data: events.append({"event": event, **data})

    def timeout(*_args, **kwargs):
        assert kwargs["timeout"] == 2
        raise subprocess.TimeoutExpired("ssh", kwargs["timeout"])

    monkeypatch.setattr("scripts.wan_matrix.subprocess.run", timeout)

    with pytest.raises(RuntimeError, match="SSH_TRANSPORT_TIMEOUT_HOSTNAME_TIMEOUT"):
        matrix.ssh(
            {"address": "190.0.231.10", "name": "matrix-test-vm", "known": "/tmp/known", "client_key_path": "/tmp/key"},
            ["hostname"],
            timeout=2,
        )

    assert events == [
        {
            "event": "ssh_failure",
            "case": "matrix-test-vm",
            "program": "hostname",
            "category": "transport_timeout",
            "exit_code": "timeout",
        }
    ]


def test_service_matrix_rejects_images_outside_its_catalog():
    with pytest.raises(ValueError, match="MATRIX_IMAGES_INVALID"):
        cases("services", images=["ubuntu-2204"])

    for image in ("ubuntu-2604", "almalinux-9", "almalinux-10", "fedora-45", "debian-13"):
        with pytest.raises(ValueError, match="MATRIX_IMAGES_INVALID"):
            cases("services", images=[image])


def test_matrix_names_are_short_sequential_and_skip_existing_api_history():
    service_cases = cases("services", images=["ubuntu-2404"])
    names = allocate_matrix_names("services", service_cases, set())
    nextcloud = [name for name in names if name.startswith("kvm-service-nextcloud-")]
    n8n = [name for name in names if name.startswith("kvm-service-n8n-")]
    assert nextcloud == [f"kvm-service-nextcloud-{n:03d}" for n in range(1, 10)]
    assert n8n == [f"kvm-service-n8n-{n:03d}" for n in range(1, 10)]
    assert all(len(name) <= 63 and name.rsplit("-", 1)[-1].isdigit() for name in names)

    second_run = allocate_matrix_names("services", service_cases, names)
    nextcloud_second = [name for name in second_run if name.startswith("kvm-service-nextcloud-")]
    n8n_second = [name for name in second_run if name.startswith("kvm-service-n8n-")]
    assert nextcloud_second == [f"kvm-service-nextcloud-{n:03d}" for n in range(10, 19)]
    assert n8n_second == [f"kvm-service-n8n-{n:03d}" for n in range(10, 19)]

    kvm_names = allocate_matrix_names("kvm", cases("kvm", images=["ubuntu-2404"]), set())
    assert kvm_names[:4] == [f"kvm-prueba-{n:03d}" for n in range(1, 5)]


def test_matrix_netbox_tag_is_the_short_vm_name():
    assert matrix_netbox_tags("kvm-service-n8n-001") == ["kvm-service-n8n-001"]
    assert matrix_netbox_tags("kvm-service-nextcloud-001") == [
        "kvm-service-nextcloud-001"
    ]
    with pytest.raises(ValueError, match="MATRIX_NETBOX_TAG_INVALID"):
        matrix_netbox_tags("wan-matrix-20261118184500")


def test_wan_matrix_repeats_kvm_combinations_four_times_for_64_vms():
    assert required_cycles("kvm") == 4
    assert required_cycles("services") == 4
    assert required_cycles("agents") == 2


def test_service_gui_matrix_covers_32_cases_over_four_cycles():
    cycles = [
        cases("services", images=["ubuntu-2404"], cycle=cycle, include_controls=False)
        for cycle in range(1, 5)
    ]
    assert all(len(cycle) == 8 for cycle in cycles)
    combined = [case for cycle in cycles for case in cycle]
    assert len(combined) == 32
    assert all(
        sum(case["product"] == product and case["plan"] == plan for case in combined)
        == 4
        for product in ("n8n", "nextcloud")
        for plan in (f"kvm-{number}" for number in range(1, 5))
    )


def test_production_matrix_requires_production_stage_and_vmid_range():
    env = {
        "IACP_RUNTIME_STAGE": "production",
        "IACP_VMID_MIN": "30000",
        "IACP_VMID_MAX": "39999",
    }
    assert matrix_runtime(env, production=True) == {
        "base_url": "https://iac.cyberfuel.com",
        "vmid_range": (30000, 39999),
        "api_container": "iacp-controlplane-prod-v1-api-1",
        "project": "iacp-controlplane-prod-v1",
    }
    with pytest.raises(RuntimeError, match="PRODUCTION_STAGE_OR_VMID_RANGE_MISMATCH"):
        matrix_runtime({**env, "IACP_VMID_MIN": "40000"}, production=True)


def test_matrix_retry_after_seconds_and_http_date_are_respected():
    from datetime import UTC, datetime, timedelta

    from email.utils import format_datetime

    from scripts.wan_matrix import retry_after_delay

    now = datetime(2026, 11, 19, 1, 0, tzinfo=UTC)
    assert retry_after_delay({"Retry-After": "5"}, 0, now=now, jitter=lambda: 0) == 5
    date = format_datetime(now + timedelta(seconds=9), usegmt=True)
    assert retry_after_delay({"Retry-After": date}, 0, now=now, jitter=lambda: 0) == 9
    assert retry_after_delay({"Retry-After": "invalid"}, 2, now=now, jitter=lambda: 0) == 4


def test_envfile_reader_parses_private_values_without_interpolation(tmp_path):
    from scripts.envfile import read_env_file

    env_file = tmp_path / "dev.env"
    env_file.write_text(
        "# comentario\nexport TOKEN=abc=xyz\nQUOTED='a # b'\nDOUBLE=\"literal $VALUE\\nsecond\"\nPLAIN=value # nota\nUNSET\n",
        encoding="utf-8",
    )
    assert read_env_file(env_file) == {
        "TOKEN": "abc=xyz",
        "QUOTED": "a # b",
        "DOUBLE": "literal $VALUE\nsecond",
        "PLAIN": "value",
        "UNSET": None,
    }


def test_service_matrix_only_targets_ubuntu_2404():
    assert {case["image"] for case in cases("services", images=["ubuntu-2404"])} == {"ubuntu-2404"}
    assert {case["image"] for case in cases("services")} == {"ubuntu-2404"}


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


def test_wan_preflight_requires_exactly_six_fresh_idle_service_workers():
    from datetime import UTC, datetime

    import pytest

    heartbeat = datetime.now(UTC).isoformat()
    state = {
        "max_concurrent": 6,
        "workers": [
            {"worker_id": f"service-worker-{index}", "status": "idle", "job_id": None, "last_heartbeat": heartbeat}
            for index in range(1, 7)
        ],
    }
    require_six_idle_service_workers(state)
    with pytest.raises(RuntimeError, match="SIX_SERVICE_WORKERS_REQUIRED"):
        require_six_idle_service_workers({"max_concurrent": 4, "workers": state["workers"][:4]})
    busy = {**state, "workers": [dict(worker) for worker in state["workers"]]}
    busy["workers"][0]["status"] = "running"
    busy["workers"][0]["job_id"] = "job-active"
    with pytest.raises(RuntimeError, match="SERVICE_WORKERS_NOT_IDLE"):
        require_six_idle_service_workers(busy)


def test_wan_preflight_uses_the_confirmed_almalinux_vmids():
    import json
    from pathlib import Path

    import pytest

    from scripts.wan_matrix import EXPECTED_ALMALINUX_IMAGES, require_expected_golden_images

    repo = Path(__file__).parents[1]
    production_catalog = json.loads((repo / "terraform/environments/mng01/catalog.json").read_text())
    development_catalog = json.loads((repo / "terraform/environments/mng01/catalog-dev.json").read_text())
    production_images = {image["image_id"]: image["template_vm_id"] for image in production_catalog["images"]}
    development_images = {image["image_id"]: image["template_vm_id"] for image in development_catalog["images"]}
    assert production_images["almalinux-9"] == 29010
    assert production_images["almalinux-10"] == 29011
    assert development_images["almalinux-9"] == 29010
    assert development_images["almalinux-10"] == 29011
    assert EXPECTED_ALMALINUX_IMAGES == {"almalinux-9": 29010, "almalinux-10": 29011}

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


def test_catalog_defaults_follow_runtime_stage(monkeypatch):
    from pathlib import Path

    from api.catalog import catalog_path
    import api.targets as targets

    repo = Path(__file__).parents[1]
    monkeypatch.delenv("IACP_MNG_CATALOG", raising=False)
    monkeypatch.setenv("IACP_DEFAULT_ENVIRONMENT", "lab")
    monkeypatch.setenv("IACP_ENABLED_ENVIRONMENTS", "lab")
    monkeypatch.setenv("IACP_RUNTIME_STAGE", "develop")
    monkeypatch.setattr(targets, "_state_backend_ready", lambda: True)
    monkeypatch.setattr(targets, "_lab_ready", lambda: True)
    expected_dev = repo / "terraform/environments/mng01/catalog-dev.json"
    assert catalog_path() == expected_dev
    assert targets.catalog_path("lab") == expected_dev

    monkeypatch.setenv("IACP_RUNTIME_STAGE", "production")
    expected_stable = repo / "terraform/environments/mng01/catalog.json"
    assert catalog_path() == expected_stable
    assert targets.catalog_path("lab") == expected_stable


def test_wan_preflight_uses_only_netbox_available_addresses():
    from ipaddress import IPv4Address
    from ipaddress import ip_address
    from types import SimpleNamespace

    class NetBoxStub:
        config = SimpleNamespace(
            expected_prefix="190.0.231.0/24",
            excluded_addresses=frozenset({ip_address("190.0.231.2")}),
        )

        def validate_gateway(self, _prefix):
            return "190.0.231.1"

        def available_addresses(self, _prefix):
            return [(IPv4Address(f"190.0.231.{host}"), 24) for host in range(2, 19)]

    assert verify_netbox_state(NetBoxStub()) == {
        "prefix": "190.0.231.0/24",
        "gateway": "190.0.231.1",
        "available_count": 9,
        "next_available": "190.0.231.10/24",
    }

    class OccupiedFirstNetBox(NetBoxStub):
        def available_addresses(self, _prefix):
            return [(IPv4Address(f"190.0.231.{host}"), 24) for host in range(11, 20)]

    assert verify_netbox_state(OccupiedFirstNetBox())["next_available"] == "190.0.231.11/24"

    class ExcludedAddressReturnedByNetBox(NetBoxStub):
        def available_addresses(self, _prefix):
            return [
                (IPv4Address("190.0.231.2"), 24),
                *[(IPv4Address(f"190.0.231.{host}"), 24) for host in range(10, 19)],
            ]

    state = verify_netbox_state(ExcludedAddressReturnedByNetBox())
    assert state["next_available"] == "190.0.231.10/24"
    assert state["available_count"] == 9

    class MissingRequiredExclusion(NetBoxStub):
        config = SimpleNamespace(
            expected_prefix="190.0.231.0/24",
            excluded_addresses=frozenset(),
        )

    with pytest.raises(RuntimeError, match="NETBOX_REQUIRED_ADDRESS_EXCLUSION_MISSING"):
        verify_netbox_state(MissingRequiredExclusion())

    class OutOfScopeNetBox(NetBoxStub):
        def available_addresses(self, _prefix):
            return [(IPv4Address("190.0.231.251"), 24)]

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
    assert compose["x-runtime-environment"]["IACP_MNG_CATALOG"] == "${IACP_MNG_CATALOG:-}"
    assert compose["x-runtime-environment"]["IACP_NETBOX_EXCLUDED_ADDRESSES"] == (
        "${IACP_NETBOX_EXCLUDED_ADDRESSES:-}"
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
    completed=[]
    matrix.record=lambda *args, **kwargs: None
    items=[{"name":str(index)} for index in range(6)]
    def fail(item):
        completed.append(item["name"])
        raise RuntimeError('FALLO_CONTROLADO')
    with pytest.raises(RuntimeError, match="MATRIX_PARALLEL_CASE_FAILURE"):
        matrix.parallel(fail,items)
    assert matrix.abort.is_set()
    assert sorted(completed)==[str(index) for index in range(6)]


def test_failed_verification_cleans_only_succeeded_ready_owned_batch():
    import threading

    from scripts.wan_matrix import Matrix

    matrix = object.__new__(Matrix)
    matrix.abort = threading.Event()
    matrix.abort.set()
    matrix.vmid_range = (40000, 49999)
    matrix.profile = "prod_static"
    instance_id = "matrix-instance"
    operation_id = "matrix-provision"
    item = {
        "name": "kvm-prueba-991",
        "instance_id": instance_id,
        "operation_id": operation_id,
        "image": "ubuntu-2604",
        "plan": "kvm-2",
        "product": "kvm",
    }
    calls = []

    def api(method, path):
        if method == "GET" and path == f"/v1/operations/{operation_id}":
            return {"status": "succeeded"}
        if method == "GET" and path == f"/v1/instances/{instance_id}":
            return {
                "instance_id": instance_id,
                "operation_id": operation_id,
                "name": item["name"],
                "image_id": item["image"],
                "plan_id": item["plan"],
                "status": "ready",
                "vmid": 40882,
                "network_allocation": {
                    "profile": "prod_static",
                    "status": "allocated",
                    "gateway": "190.0.231.1",
                    "address": "190.0.231.20/24",
                },
            }
        raise AssertionError((method, path))

    matrix.api = api
    matrix.idle = lambda: calls.append(("idle", None))
    matrix.cleanup = lambda target: calls.append(("cleanup", target["name"]))
    matrix.post_cleanup = lambda targets: calls.append(("post_cleanup", len(targets)))
    matrix.record = lambda event, **values: calls.append((event, values))

    matrix.cleanup_failed_verification_batch(
        [item], RuntimeError("SSH_CONNECTION_REFUSED_HOSTNAME_255")
    )

    assert item["vmid"] == 40882
    assert item["address"] == "190.0.231.20"
    assert ("cleanup", item["name"]) in calls
    assert ("post_cleanup", 1) in calls
    assert any(
        event == "failed_batch_cleanup" and values["result"] == "PASS"
        for event, values in calls
    )
    assert matrix.abort.is_set()


def test_failed_verification_keeps_uncertain_batch_fail_closed():
    import threading

    import pytest

    from scripts.wan_matrix import Matrix

    matrix = object.__new__(Matrix)
    matrix.abort = threading.Event()
    matrix.abort.set()
    matrix.vmid_range = (40000, 49999)
    matrix.profile = "prod_static"
    item = {
        "name": "kvm-prueba-992",
        "instance_id": "matrix-instance",
        "operation_id": "matrix-provision",
        "image": "ubuntu-2604",
        "plan": "kvm-2",
        "product": "kvm",
    }
    cleaned = []
    events = []
    matrix.api = lambda *_args: {"status": "uncertain"}
    matrix.idle = lambda: None
    matrix.cleanup = lambda target: cleaned.append(target)
    matrix.record = lambda event, **values: events.append((event, values))

    with pytest.raises(RuntimeError, match="MATRIX_FAILURE_CLEANUP_BLOCKED"):
        matrix.cleanup_failed_verification_batch(
            [item], RuntimeError("SSH_CONNECTION_REFUSED_HOSTNAME_255")
        )

    assert cleaned == []
    assert matrix.abort.is_set()
    assert any(
        event == "failed_batch_cleanup"
        and values["result"] == "BLOCKED"
        and values["cleanup_error"] == "FAILED_BATCH_OPERATION_NOT_SUCCEEDED"
        for event, values in events
    )


def test_proxmox_vm_config_retries_rate_limit_for_safe_gets(monkeypatch):
    from types import SimpleNamespace

    from worker.proxmox import ProxmoxError

    matrix = object.__new__(Matrix)
    calls = []
    events = []

    def vm_config(node, vmid):
        calls.append((node, vmid))
        if len(calls) < 3:
            raise ProxmoxError("private response detail", 429)
        return {"name": "kvm-service-n8n-040"}

    matrix.proxmox = SimpleNamespace(vm_config=vm_config)
    matrix.record = lambda event, **details: events.append((event, details))
    monkeypatch.setattr("scripts.wan_matrix.time.sleep", lambda _delay: None)

    result = matrix.proxmox_vm_config("mng01", 30005)

    assert result == {"name": "kvm-service-n8n-040"}
    assert calls == [("mng01", 30005)] * 3
    assert [event for event, _ in events] == [
        "proxmox_read_retry",
        "proxmox_read_retry",
    ]
    assert all(details["status"] == 429 for _, details in events)


def test_parallel_records_safe_proxmox_status_without_exception_message():
    import threading

    import pytest

    from worker.proxmox import ProxmoxError

    matrix = object.__new__(Matrix)
    matrix.abort = threading.Event()
    events = []
    matrix.record = lambda event, **details: events.append((event, details))

    def fail(_item):
        raise ProxmoxError("sensitive response detail", 403)

    with pytest.raises(RuntimeError, match="MATRIX_PARALLEL_CASE_FAILURE"):
        matrix.parallel(fail, [{"name": "kvm-service-n8n-040"}])

    error = next(details for event, details in events if event == "case_error")
    assert error == {
        "case": "kvm-service-n8n-040",
        "error": "PROXMOX_HTTP_403",
        "http_status": 403,
        "uncertain": False,
    }
    assert "sensitive" not in repr(error)


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


@pytest.mark.parametrize("image", ["almalinux-9", "almalinux-10"])
def test_almalinux_cloud_init_status_uses_authenticated_ssh_not_qga(image):
    import json

    matrix = object.__new__(Matrix)
    commands = []
    checks = []

    def ssh(_item, argv, **kwargs):
        commands.append((argv, kwargs))
        return json.dumps({"status": "done", "errors": []})

    matrix.ssh = ssh
    matrix.qga = lambda *_args, **_kwargs: pytest.fail(
        "AlmaLinux cloud-init status must not execute through Guest Agent"
    )
    matrix.check = lambda item, check_id, evidence: checks.append(
        (item["name"], check_id, evidence)
    )

    matrix.verify_cloud_init({"name": "kvm-prueba-013", "image": image})

    assert len(commands) == 1
    argv, kwargs = commands[0]
    assert argv[:2] == ["sh", "-c"]
    assert "/usr/bin/cloud-init status --long --format json" in argv[2]
    assert kwargs == {"timeout": 60}
    assert checks[0][1] == "A04"
    assert "SSH autenticado" in checks[0][2]
    assert "SELinux" in checks[0][2]


def test_almalinux_cloud_init_still_rejects_errors_reported_over_ssh():
    import json

    matrix = object.__new__(Matrix)
    matrix.ssh = lambda *_args, **_kwargs: json.dumps(
        {"status": "error", "errors": ["cloud-init module failed"]}
    )
    matrix.qga = lambda *_args, **_kwargs: pytest.fail(
        "AlmaLinux cloud-init status must not execute through Guest Agent"
    )
    matrix.check = lambda *_args, **_kwargs: pytest.fail(
        "Failed cloud-init must not record a passing check"
    )

    with pytest.raises(AssertionError, match="CLOUD_INIT_NOT_COMPLETE"):
        matrix.verify_cloud_init({"name": "kvm-prueba-013", "image": "almalinux-9"})


def test_ubuntu_cloud_init_status_remains_on_guest_agent():
    import json

    matrix = object.__new__(Matrix)
    calls = []
    matrix.qga = lambda vmid, argv, **kwargs: calls.append(
        (vmid, argv, kwargs)
    ) or json.dumps({"status": "done", "errors": []})
    matrix.ssh = lambda *_args, **_kwargs: pytest.fail(
        "Ubuntu cloud-init status remains checked through Guest Agent"
    )
    matrix.check = lambda *_args, **_kwargs: None

    matrix.verify_cloud_init({"name": "kvm-prueba-013", "image": "ubuntu-2404", "vmid": 40013})

    assert calls == [
        (
            40013,
            ["/usr/bin/cloud-init", "status", "--long", "--format", "json"],
            {"allowed_exitcodes": (0, 2)},
        )
    ]


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


def test_wait_service_retries_initial_ssh_failure_after_worker_release(monkeypatch):
    import scripts.wan_matrix as wan_matrix

    matrix = object.__new__(wan_matrix.Matrix)
    job_id = "ssh-connectivity-job"
    states = iter(
        [
            {
                "job_id": job_id,
                "status": "failed",
                "error": "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SSH_CONNECTIVITY",
                "worker_id": "service-worker-2",
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
                        "worker_id": "service-worker-2",
                        "status": "idle",
                        "job_id": None,
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
    assert any(
        call[:2] == ("POST", f"/v1/service-jobs/{job_id}/retry")
        for call in calls
    )


def test_wait_service_retries_egress_preflight_failure_after_worker_release(monkeypatch):
    import scripts.wan_matrix as wan_matrix

    matrix = object.__new__(wan_matrix.Matrix)
    job_id = "egress-preflight-job"
    states = iter(
        [
            {
                "job_id": job_id,
                "status": "failed",
                "error": "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SERVICE_EGRESS_PREFLIGHT",
                "worker_id": "service-worker-1",
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
                        "worker_id": "service-worker-1",
                        "status": "idle",
                        "job_id": None,
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
    assert any(
        call[:2] == ("POST", f"/v1/service-jobs/{job_id}/retry")
        and call[2] == {"previous_execution_stopped": True}
        and call[3] == f"matrix-service-retry-{job_id}"
        for call in calls
    )


def test_wait_service_waits_for_worker_heartbeat_before_retry(monkeypatch):
    import scripts.wan_matrix as wan_matrix

    matrix = object.__new__(wan_matrix.Matrix)
    job_id = "ssh-handoff-race-job"
    states = iter(
        [
            {
                "job_id": job_id,
                "status": "failed",
                "error": "SERVICE_EXECUTION_FAILED:ANSIBLE_FAILED_SSH_CONNECTIVITY",
                "worker_id": "service-worker-3",
            },
            {"job_id": job_id, "status": "succeeded", "error": ""},
        ]
    )
    worker_states = iter(
        [
            {"worker_id": "service-worker-3", "status": "running", "job_id": job_id},
            {"worker_id": "service-worker-3", "status": "idle", "job_id": None},
        ]
    )
    calls = []

    def api(method, path, body=None, key=None):
        calls.append((method, path, body, key))
        if path == f"/v1/service-jobs/{job_id}":
            return next(states)
        if path == "/v1/service-workers":
            return {"workers": [next(worker_states)]}
        if path == f"/v1/service-jobs/{job_id}/retry":
            return {"job_id": job_id, "status": "queued"}
        raise AssertionError(path)

    matrix.api = api
    matrix.record = lambda *_args, **_kwargs: None
    monkeypatch.setattr(wan_matrix.time, "sleep", lambda _seconds: None)
    matrix.wait_service(job_id)
    assert sum(path == "/v1/service-workers" for _, path, *_ in calls) == 2
    assert any(path.endswith("/retry") for _, path, *_ in calls)


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


def test_settle_service_batch_cancels_uncertain_only_after_worker_release():
    from scripts.wan_matrix import Matrix

    matrix = object.__new__(Matrix)
    job_id = "uncertain-service-job"
    item = {"job_id": job_id, "name": "kvm-service-nextcloud-001"}
    calls = []
    records = []

    def api(method, path, body=None, key=None):
        calls.append((method, path, body, key))
        if method == "GET" and path == f"/v1/service-jobs/{job_id}":
            return {"job_id": job_id, "status": "uncertain", "worker_id": "service-worker-2"}
        if method == "POST" and path == f"/v1/service-jobs/{job_id}/cancel":
            return {"job_id": job_id, "status": "cancelled"}
        raise AssertionError(path)

    matrix.api = api
    matrix.wait_for_service_worker_release = lambda actual_job, worker: calls.append(
        ("WORKER_RELEASED", actual_job, worker, None)
    )
    matrix.record = lambda event, **values: records.append((event, values))

    matrix.settle_service_batch([item])

    assert calls[1] == ("WORKER_RELEASED", job_id, "service-worker-2", None)
    assert calls[2] == (
        "POST",
        f"/v1/service-jobs/{job_id}/cancel",
        {"previous_execution_stopped": True},
        f"matrix-recovery-cancel-{job_id}",
    )
    assert records[0][0] == "service_recovery_cancelled"
    assert records[0][1]["case"] == item["name"]


def test_settle_service_batch_waits_for_async_cleanup_after_cancel():
    from scripts.wan_matrix import Matrix

    matrix = object.__new__(Matrix)
    job_id = "uncertain-service-job"
    cleanup_job_id = "service-cleanup-job"
    service_id = "service-test"
    item = {
        "job_id": job_id,
        "name": "kvm-service-nextcloud-001",
        "service_id": service_id,
    }
    calls = []
    records = []

    def api(method, path, body=None, key=None):
        calls.append((method, path, body, key))
        if method == "GET" and path == f"/v1/service-jobs/{job_id}":
            return {"job_id": job_id, "status": "uncertain", "worker_id": "service-worker-2"}
        if method == "POST" and path == f"/v1/service-jobs/{job_id}/cancel":
            return {
                "job_id": cleanup_job_id,
                "cancelled_job_id": job_id,
                "status": "queued",
            }
        if method == "GET" and path == f"/v1/service-instances/{service_id}":
            return {"service_id": service_id, "status": "cancelled"}
        raise AssertionError(path)

    matrix.api = api
    matrix.wait_for_service_worker_release = lambda actual_job, worker: calls.append(
        ("WORKER_RELEASED", actual_job, worker, None)
    )
    matrix.wait_service = lambda actual_job: calls.append(
        ("CLEANUP_JOB_SUCCEEDED", actual_job, None, None)
    )
    matrix.record = lambda event, **values: records.append((event, values))

    matrix.settle_service_batch([item])

    assert calls[1] == ("WORKER_RELEASED", job_id, "service-worker-2", None)
    assert calls[2] == (
        "POST",
        f"/v1/service-jobs/{job_id}/cancel",
        {"previous_execution_stopped": True},
        f"matrix-recovery-cancel-{job_id}",
    )
    assert calls[3] == ("CLEANUP_JOB_SUCCEEDED", cleanup_job_id, None, None)
    assert calls[4][0:2] == (
        "GET",
        f"/v1/service-instances/{service_id}",
    )
    assert records == [
        (
            "service_recovery_cleanup_completed",
            {
                "case": item["name"],
                "job_id": job_id,
                "cleanup_job_id": cleanup_job_id,
                "previous_execution_stopped": True,
            },
        )
    ]


def test_failed_service_batch_cleans_every_instance_before_reporting_failure():
    import pytest

    from scripts.wan_matrix import Matrix

    matrix = object.__new__(Matrix)
    items = [
        {"name": "kvm-service-n8n-001", "product": "n8n", "job_id": "job-1"},
        {"name": "kvm-service-nextcloud-001", "product": "nextcloud", "job_id": "job-2"},
    ]
    cleaned = []
    events = []

    def wait_service(job_id):
        if job_id == "job-1":
            raise RuntimeError("SERVICE_UNCERTAIN_TEST")

    matrix.wait_service = wait_service
    matrix.record = lambda event, **values: events.append((event, values))
    matrix.settle_service_batch = lambda target: events.append(("settled", len(target)))
    matrix.cleanup = lambda item: cleaned.append(item["name"])
    matrix.parallel = lambda function, target: [function(item) for item in target]
    matrix.post_cleanup = lambda target: events.append(("post_cleanup", len(target)))
    matrix.idle = lambda: events.append(("idle", True))

    with pytest.raises(RuntimeError, match="SERVICE_BATCH_FAILED_AFTER_CLEANUP"):
        matrix.process_service_batch(items)

    assert cleaned == [item["name"] for item in items]
    assert [event[0] for event in events] == [
        "service_case_error",
        "settled",
        "post_cleanup",
        "idle",
    ]


def test_cleanup_hydrates_vmid_and_static_ip_from_exact_instance(monkeypatch):
    import io

    import scripts.wan_matrix as wan_matrix
    from scripts.wan_matrix import Matrix

    service_id = "service-test-id"
    instance_id = "instance-test-id"
    operation_id = "delete-operation-id"
    calls = []
    checks = []
    before = {
        "instance_id": instance_id,
        "name": "kvm-service-nextcloud-001",
        "vmid": 30042,
        "image_id": "ubuntu-2404",
        "plan_id": "kvm-3",
        "status": "ready",
        "network_allocation": {
            "profile": "prod_static",
            "status": "allocated",
            "address": "190.0.231.25/24",
            "gateway": "190.0.231.1",
        },
    }
    after = {**before, "status": "deleted", "network_allocation": {"status": "released"}}

    def api(method, path, body=None, key=None):
        calls.append((method, path, body, key))
        if method == "GET" and path == f"/v1/instances/{instance_id}":
            return before if sum(call[0] == "GET" and call[1] == path for call in calls) == 1 else after
        if method == "GET" and path == f"/v1/service-instances/{service_id}":
            return {
                "service_id": service_id,
                "instance_id": instance_id,
                "application_id": "nextcloud",
            }
        if method == "POST" and path.endswith("/power/shutdown"):
            return {"operation_id": "shutdown-operation-id"}
        if method == "DELETE" and path == f"/v1/service-instances/{service_id}":
            return {"operation_id": operation_id}
        if method == "GET" and path.startswith("/v1/events?"):
            return {"events": [{"kind": "logical.delete.completed", "status": "succeeded"}]}
        raise AssertionError(path)

    class EmptyNetBoxResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def urlopen(request, timeout):
        assert timeout == 30
        assert "address=190.0.231.25/24" in request.full_url
        return EmptyNetBoxResponse(b'{"count": 0}')

    monkeypatch.setattr(wan_matrix, "urlopen", urlopen)
    matrix = object.__new__(Matrix)
    matrix.api = api
    matrix.vmid_range = (30000, 39999)
    matrix.profile = "prod_static"
    matrix.env = {"IACP_NETBOX_TOKEN": "test-only", "IACP_NETBOX_URL": "https://netbox.example.invalid"}
    matrix.wait_operation = lambda _operation: None
    matrix.proxmox = type("Proxmox", (), {"inventory": lambda _self: []})()
    matrix.check = lambda item, code, detail: checks.append((item["name"], code, detail))
    item = {
        "name": "kvm-service-nextcloud-001",
        "product": "nextcloud",
        "service_id": service_id,
        "instance_id": instance_id,
        "operation_id": "create-operation-id",
        "image": "ubuntu-2404",
        "plan": "kvm-3",
    }

    matrix.cleanup(item)

    assert item["vmid"] == 30042
    assert item["address"] == "190.0.231.25"
    assert ("DELETE", f"/v1/service-instances/{service_id}", None, f"delete-{item['name']}") in calls
    assert {code for _name, code, _detail in checks} == {"E01", "E02", "E03", "E04", "E06"}


def _matriz_sin_inicializar():
    from scripts.wan_matrix import Matrix

    return object.__new__(Matrix)


def test_idle_tolera_un_estado_transitorio_y_se_recupera(monkeypatch):
    import scripts.wan_matrix as wan_matrix

    matriz = _matriz_sin_inicializar()
    intentos = {"n": 0}

    def una_vez():
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise RuntimeError("SERVICE_QUEUE_NOT_IDLE")  # lock advisory de NetBox en curso

    matriz._idle_once = una_vez
    monkeypatch.setattr(wan_matrix.time, "sleep", lambda _s: None)
    monkeypatch.setenv("IACP_MATRIX_IDLE_WAIT_SECONDS", "90")
    matriz.idle()
    assert intentos["n"] == 3


def test_idle_sigue_fallando_si_el_estado_persiste(monkeypatch):
    import pytest

    import scripts.wan_matrix as wan_matrix

    matriz = _matriz_sin_inicializar()
    matriz._idle_once = lambda: (_ for _ in ()).throw(AssertionError("RUNTIME_NOT_IDLE"))
    reloj = iter(range(0, 1000, 40))
    monkeypatch.setattr(wan_matrix.time, "monotonic", lambda: next(reloj))
    monkeypatch.setattr(wan_matrix.time, "sleep", lambda _s: None)
    monkeypatch.setenv("IACP_MATRIX_IDLE_WAIT_SECONDS", "90")
    with pytest.raises(AssertionError, match="RUNTIME_NOT_IDLE"):
        matriz.idle()
