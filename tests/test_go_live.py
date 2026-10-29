import base64
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import func, select

from iacp.models import Job, Operation

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = load("go_live", "go_live.py")
tls = load("go_live_tls", "render-tls.py")


def _verification_promotion(*, admission=0, lifecycle=0, planned_deletion=None,
                            actual_deletion=None):
    """Construye un Promotion aislado para ejercitar verify sin Docker físico."""
    planned_deletion = str(0 if planned_deletion is None else planned_deletion)
    actual_deletion = planned_deletion if actual_deletion is None else str(actual_deletion)
    images = {"api": "api-image", "terraform": "terraform-image"}
    planned_environment = {
        "IACP_ADMISSION_ENABLED": str(admission),
        "IACP_KVM_LIFECYCLE_ENABLED": str(lifecycle),
        "IACP_TERRAFORM_APPLY_ENABLED": "1",
        "IACP_TERRAFORM_DELETE_ENABLED": planned_deletion,
    }
    config = {"services": {
        role: {"environment": dict(planned_environment)} for role in driver.ROLES
    }}
    containers = {}
    for role in driver.ROLES:
        values = dict(planned_environment)
        values["IACP_TERRAFORM_DELETE_ENABLED"] = actual_deletion
        containers[role] = {
            "State": {"Running": True},
            "Image": images["terraform" if role == "terraform-worker" else "api"],
            "Config": {"Env": [f"{key}={value}" for key, value in values.items()], "Labels": {}},
            "HostConfig": {"PortBindings": {}, "ReadonlyRootfs": role == "lifecycle-worker"},
            "Mounts": [],
        }
    promotion = object.__new__(driver.Promotion)
    promotion.m = {"images": images}
    promotion.compose = Mock(return_value=json.dumps(config))
    promotion.canonical = Mock(return_value=False)
    promotion.container = Mock(side_effect=containers.__getitem__)
    promotion.planned_mounts = Mock(return_value={})
    promotion.lifecycle_mount_targets = Mock(return_value=set())
    promotion.evidence = Mock()
    promotion.revalidate_operation_fences = Mock()
    promotion.workers_idle = Mock()
    promotion.snapshot = Mock(return_value={
        "schema": [driver.HEAD],
        "nonterminal": {
            "operations": [], "jobs": [], "guest_initializations": [],
            "terraform_applies": [], "power_executions": [],
            "deletion_executions": [],
        },
        "queues": {}, "unacked": 0,
    })

    def exec_python(_role, code):
        if "SOURCE_REVISION" in code:
            return json.dumps({"source": driver.SOURCE, "terraform": "1.16.6"})
        if "ownership_revision" in code:
            return json.dumps(driver.SOURCE)
        return ""

    promotion.exec_python = Mock(side_effect=exec_python)
    return promotion


def _canonical_promotion(*, operation_id=None):
    promotion = object.__new__(driver.Promotion)
    api_image, terraform_image = "api-image", "terraform-image"
    operation_id = operation_id or "4652c4aa-58bb-4f92-94c9-c81790e74150"
    env = {
        "IACP_ADMISSION_ENABLED": "0",
        "IACP_KVM_LIFECYCLE_ENABLED": "0",
        "IACP_TERRAFORM_APPLY_ENABLED": "1",
        "IACP_TERRAFORM_DELETE_ENABLED": "0",
    }
    containers = {}
    for role in driver.ROLES:
        containers[role] = {
            "Image": terraform_image if role == "terraform-worker" else api_image,
            "Config": {"Env": [f"{key}={value}" for key, value in env.items()], "Labels": {}},
        }
    promotion.m = {"project": "iacp-controlplane", "images": {
        "api": api_image, "terraform": terraform_image,
    }}
    promotion.execution_profile = {"environment": "lab"}
    promotion.authorize = Mock(return_value={"reference": "lab:test:deploy"})
    promotion.preflight = Mock(return_value={"schema": [driver.HEAD]})
    promotion.reviewed_operation_fences = ({"operation_id": operation_id},)
    promotion.review_recovery_deployment = Mock(return_value=())
    promotion.canonical = Mock(return_value=True)
    promotion.container = Mock(side_effect=containers.__getitem__)
    promotion.migrate_technical_ownership = Mock(return_value={"blocked_count": 0})
    promotion.verify = Mock()
    promotion.flags = Mock()
    promotion.up = Mock()
    promotion.evidence = Mock()
    return promotion


def test_verify_signature_exposes_the_explicit_delete_contract():
    parameters = inspect.signature(driver.Promotion.verify).parameters
    assert parameters["deletion"].default is None
    assert {"admission", "lifecycle", "deletion", "idle",
            "allowed_uncertain", "allowed_operation_uncertain"} <= set(parameters)


@pytest.mark.parametrize("admission,lifecycle,deletion", [
    (0, 0, 0),
    (1, 1, 1),
    (1, 0, 1),
    (0, 1, 0),
])
def test_verify_checks_apply_and_all_runtime_capability_flags(admission, lifecycle, deletion):
    promotion = _verification_promotion(
        admission=admission, lifecycle=lifecycle,
        planned_deletion=deletion, actual_deletion=deletion,
    )
    driver.Promotion.verify(
        promotion, admission=admission, lifecycle=lifecycle, deletion=deletion,
    )


def test_verify_preserves_compose_delete_default_for_existing_callers():
    promotion = _verification_promotion(
        admission=0, lifecycle=0, planned_deletion=1, actual_deletion=1,
    )
    driver.Promotion.verify(promotion, admission=0, lifecycle=0)


@pytest.mark.parametrize("planned_deletion,actual_deletion,expected", [
    ("0", "1", "solicitado"),
    ("1", "0", "efectivo"),
])
def test_verify_rejects_delete_contract_drift(planned_deletion, actual_deletion, expected):
    promotion = _verification_promotion(
        admission=0, lifecycle=0,
        planned_deletion=planned_deletion, actual_deletion=actual_deletion,
    )
    with pytest.raises(RuntimeError, match=expected):
        driver.Promotion.verify(promotion, admission=0, lifecycle=0, deletion=1)


def test_verify_rejects_invalid_delete_contract():
    promotion = _verification_promotion(admission=0, lifecycle=0)
    with pytest.raises(RuntimeError, match="Indicador DELETE inválido"):
        driver.Promotion.verify(promotion, admission=0, lifecycle=0, deletion=2)
    promotion.compose.assert_not_called()


def test_promote_passes_restored_delete_to_final_canonical_verification():
    promotion = _canonical_promotion()
    driver.Promotion.promote(promotion)
    assert promotion.verify.call_count == 2
    for call in promotion.verify.call_args_list:
        assert call.kwargs["allowed_operation_uncertain"] == (
            "4652c4aa-58bb-4f92-94c9-c81790e74150",
        )
    assert promotion.verify.call_args_list[1].kwargs["deletion"] == 0
    promotion.flags.assert_called_once_with(admission=0, lifecycle=0, deletion=0)
    promotion.up.assert_called_once_with(*driver.ROLES)
    promotion.migrate_technical_ownership.assert_called_once_with()
    assert any(call.args[0] == "canonical-adoption" for call in promotion.evidence.call_args_list)


def test_promote_passes_restored_delete_through_the_normal_path():
    promotion = object.__new__(driver.Promotion)
    before = {
        "schema": ["0014_allow_historical_kvm_106"],
        "nonterminal": {
            "operations": [], "jobs": [], "guest_initializations": [],
            "terraform_applies": [], "power_executions": [],
            "deletion_executions": [],
        },
        "queues": {}, "unacked": 0, "tables": {},
    }
    after = {**before, "schema": [driver.HEAD]}
    api = {"Config": {"Env": [
        "IACP_ADMISSION_ENABLED=0",
        "IACP_KVM_LIFECYCLE_ENABLED=0",
        "IACP_TERRAFORM_DELETE_ENABLED=1",
    ]}}
    promotion.m = {"project": "iacp-controlplane"}
    promotion.docker = []
    promotion.run = Mock(side_effect=["id", "[]"])
    promotion.authorize = Mock(return_value={"reference": "lab:test:deploy"})
    promotion.preflight = Mock(return_value=before)
    promotion.review_recovery_deployment = Mock(return_value=())
    promotion.reviewed_operation_fences = ()
    promotion.canonical = Mock(return_value=False)
    promotion.container = Mock(return_value=api)
    promotion.compose = Mock(return_value="")
    promotion.flags = Mock()
    promotion.snapshot = Mock(return_value=before)
    promotion.snapshot_stopped = Mock(return_value=before)
    promotion.revalidate_operation_fences = Mock()
    promotion.workers_idle = Mock()
    promotion.up = Mock()
    promotion.migrate_technical_ownership = Mock(return_value={"blocked_count": 0})
    promotion.verify = Mock(side_effect=[None, after])
    promotion.evidence = Mock()

    driver.Promotion.promote(promotion)

    assert promotion.verify.call_args_list[1].kwargs["deletion"] == 1
    promotion.flags.assert_any_call(admission=0, lifecycle=0, deletion=0)
    promotion.flags.assert_any_call(admission=0, lifecycle=0, deletion=1)


def test_promote_keeps_partial_runtime_fail_closed_on_final_verification_error():
    promotion = _canonical_promotion()
    promotion.verify.side_effect = [None, RuntimeError("health check failed")]
    with pytest.raises(RuntimeError, match="health check failed"):
        driver.Promotion.promote(promotion)
    promotion.flags.assert_called_once_with(admission=0, lifecycle=0, deletion=0)
    assert promotion.up.call_count == 1


def test_operation_fence_uses_terraform_worker_after_workers_are_drained():
    promotion = object.__new__(driver.Promotion)
    promotion.container = Mock(return_value={"State": {"Running": False}})
    promotion.compose = Mock(return_value=json.dumps({"passed": True}))
    review = {"operation_id": "4652c4aa-58bb-4f92-94c9-c81790e74150"}

    result = driver.Promotion._run_operation_fence_probe(promotion, review)

    assert result["passed"] is True
    args, kwargs = promotion.compose.call_args
    assert args[-2:] == ("terraform-worker", "-")
    assert kwargs["input"]


def test_promote_is_idempotent_from_schema_head_partial_runtime():
    promotion = _canonical_promotion()
    driver.Promotion.promote(promotion)
    driver.Promotion.promote(promotion)
    assert promotion.migrate_technical_ownership.call_count == 2
    assert promotion.verify.call_count == 4
    assert promotion.up.call_count == 2
    promotion.flags.assert_any_call(admission=0, lifecycle=0, deletion=0)


def test_promotion_accepts_the_deployed_0010_schema_before_migrating():
    driver.require_supported_schema(['0010_guest_identity'])
    driver.require_supported_schema(['0013_technical_ownership'])
    driver.require_supported_schema([driver.HEAD])
    for unknown in ([], ['future'], ['0010_guest_identity', driver.HEAD]):
        with pytest.raises(RuntimeError, match='Unsupported schema'):
            driver.require_supported_schema(unknown)


def test_restore_evidence_accepts_actual_0011_backup_and_rejects_unverified_restore():
    backup = {'schema': '0011_crm_contract', 'sha256': 'digest-comprobado'}
    restored = {'schema': '0011_crm_contract', 'backup_sha256': 'digest-comprobado',
        'passed': True, 'isolated': True, 'table_count': 15}
    driver.check_restore_evidence(backup, restored)
    for change in ({'schema': driver.HEAD}, {'backup_sha256': 'otro'},
                   {'table_count': 14}, {'passed': False}, {'isolated': False}):
        with pytest.raises(RuntimeError, match='Restore attestation'):
            driver.check_restore_evidence(backup, {**restored, **change})


def test_restore_evidence_accepts_actual_0013_backup_before_0014():
    backup = {'schema': '0013_technical_ownership', 'sha256': 'digest-0013'}
    restored = {'schema': '0013_technical_ownership', 'backup_sha256': 'digest-0013',
                'passed': True, 'isolated': True, 'table_count': 17}
    driver.check_restore_evidence(backup, restored)
    with pytest.raises(RuntimeError, match='Restore attestation'):
        driver.check_restore_evidence(backup, {**restored, 'table_count': 16})


def test_lab_execution_profile_is_host_bound_and_generates_audit_authorization():
    profile = {
        "version": 1, "environment": "lab", "cluster": "MNG",
        "control_plane": "VM104", "hostname": "iacp-controlplane-01",
        "enabled": True,
    }
    validated = driver.validate_host_execution_profile(
        profile, owner_uid=0, mode=0o600, effective_uid=0,
        hostname="iacp-controlplane-01", actual_hostname="iacp-controlplane-01",
    )
    assert validated["environment"] == "lab"

    promotion = object.__new__(driver.Promotion)
    promotion.m = {"project": "iacp-controlplane", "release": "a" * 40}
    promotion.execution_profile = {"environment": "lab"}
    evidence = []
    promotion.evidence = lambda name, value: evidence.append((name, value))
    result = promotion.authorize("deploy")
    assert result["authorized"] is True and result["approved"] is False
    assert result["authorization_source"] == "host-execution-profile"
    assert evidence[0][0] == "lab-authorization"


def test_lab_authorizes_activate_core_but_production_requires_approval():
    promotion = object.__new__(driver.Promotion)
    promotion.m = {"project": "iacp-controlplane", "release": "a" * 40}
    promotion.execution_profile = {"environment": "lab"}
    evidence = []
    promotion.evidence = lambda name, value: evidence.append((name, value))
    result = promotion.authorize("activate-core")
    assert result["authorized"] is True and result["approved"] is False
    assert evidence[0][1]["action"] == "activate-core"

    production = object.__new__(driver.Promotion)
    production.m = promotion.m
    production.execution_profile = {"environment": "production"}
    production.approve = lambda action: ("approval", action)
    assert production.authorize("activate-core") == ("approval", "activate-core")


def test_lab_authorizes_only_the_exact_consumed_bootstrap_recovery_action():
    promotion = object.__new__(driver.Promotion)
    promotion.m = {"project": "iacp-controlplane", "release": "a" * 40}
    promotion.execution_profile = {"environment": "lab"}
    evidence = []
    promotion.evidence = lambda name, value: evidence.append((name, value))
    result = promotion.authorize("recover-consumed-commercial-bootstrap")
    assert result["authorized"] is True and result["approved"] is False
    assert evidence[0][1]["action"] == "recover-consumed-commercial-bootstrap"

    production = object.__new__(driver.Promotion)
    production.m = promotion.m
    production.execution_profile = {"environment": "production"}
    production.approve = lambda action: ("approval", action)
    assert production.authorize("recover-consumed-commercial-bootstrap") == (
        "approval", "recover-consumed-commercial-bootstrap"
    )


def test_lab_profile_rejects_an_unauthorized_os_identity():
    profile = {
        "version": 1, "environment": "lab", "cluster": "MNG",
        "control_plane": "VM104", "hostname": "iacp-controlplane-01",
        "enabled": True,
    }
    with pytest.raises(RuntimeError, match="authorized OS identity"):
        driver.validate_host_execution_profile(
            profile, owner_uid=0, mode=0o600, effective_uid=1000,
            hostname="iacp-controlplane-01", actual_hostname="iacp-controlplane-01",
        )


def test_invalid_host_lab_profile_fails_closed():
    profile = {
        "version": 1, "environment": "lab", "cluster": "PRX",
        "control_plane": "VM104", "hostname": "iacp-controlplane-01",
        "enabled": True,
    }
    with pytest.raises(RuntimeError, match="authorized MNG LAB profile"):
        driver.validate_host_execution_profile(
            profile, owner_uid=0, mode=0o600, effective_uid=0,
            hostname="iacp-controlplane-01", actual_hostname="iacp-controlplane-01",
        )


def test_compare_history_0013_to_0014_allows_only_reported_ownership_rows():
    before = {
        'schema': ['0013_technical_ownership'],
        'tables': {
            'technical_ownership': {'count': 1, 'sha256': 'before'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
            'operations': {'count': 3, 'sha256': 'operations'},
        },
        'technical_ownership_entries': {'existing': 'digest-existing'},
    }
    after = {
        'schema': [driver.HEAD],
        'tables': {
            'technical_ownership': {'count': 2, 'sha256': 'after'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
            'operations': {'count': 3, 'sha256': 'operations'},
        },
        'technical_ownership_entries': {
            'existing': 'digest-existing', 'adopted': 'digest-adopted',
        },
    }
    migration = {'adopted_instances': [{'instance_id': 'adopted', 'vmid': 106}]}
    driver.compare_history(before, after, migration)
    with pytest.raises(RuntimeError, match='Existing technical ownership changed'):
        driver.compare_history(before, {
            **after,
            'technical_ownership_entries': {'existing': 'rewritten', 'adopted': 'digest-adopted'},
        }, migration)
    with pytest.raises(RuntimeError, match='Unexpected technical ownership rows'):
        driver.compare_history(before, {
            **after,
            'technical_ownership_entries': {
                **after['technical_ownership_entries'], 'unexpected': 'digest-unexpected',
            },
        }, migration)


def test_compare_history_0014_to_0015_allows_only_neutral_technical_access_columns():
    before = {
        'schema': ['0014_allow_historical_kvm_106'],
        'tables': {
            'instances': {'count': 1, 'sha256': 'instances'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
        },
    }
    after = {
        'schema': [driver.HEAD],
        'tables': {
            'instances': {'count': 1, 'sha256': 'with-technical-fields'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
        },
        'compatibility': {
            'instances_before_0015': {
                'count': 1, 'sha256': 'instances', 'new_fields_default': True,
            },
        },
    }
    driver.compare_history(before, after)
    with pytest.raises(RuntimeError, match='technical access migration'):
        driver.compare_history(before, {
            **after,
            'compatibility': {
                'instances_before_0015': {
                    'count': 1, 'sha256': 'rewritten', 'new_fields_default': True,
                },
            },
        })
    with pytest.raises(RuntimeError, match='technical access migration'):
        driver.compare_history(before, {
            **after,
            'compatibility': {
                'instances_before_0015': {
                    'count': 1, 'sha256': 'instances', 'new_fields_default': False,
                },
            },
        })


@pytest.mark.parametrize('changed', [
    {'count': 3, 'sha256': 'deletions'},
    {'count': 2, 'sha256': 'rewritten'},
])
def test_compare_history_0013_to_0014_rejects_changed_historical_deletions(changed):
    before = {
        'schema': ['0013_technical_ownership'],
        'tables': {
            'technical_ownership': {'count': 0, 'sha256': 'before'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
        },
        'technical_ownership_entries': {},
    }
    after = {
        'schema': [driver.HEAD],
        'tables': {
            'technical_ownership': {'count': 0, 'sha256': 'after'},
            'deletion_executions': changed,
        },
        'technical_ownership_entries': {},
    }
    with pytest.raises(RuntimeError, match='Historical rows changed: deletion_executions'):
        driver.compare_history(before, after, {'adopted_instances': []})


def test_compare_history_0013_to_0014_rejects_missing_historical_table():
    before = {
        'schema': ['0013_technical_ownership'],
        'tables': {
            'technical_ownership': {'count': 0, 'sha256': 'before'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
        },
        'technical_ownership_entries': {},
    }
    after = {
        'schema': [driver.HEAD],
        'tables': {'technical_ownership': {'count': 0, 'sha256': 'after'}},
        'technical_ownership_entries': {},
    }
    with pytest.raises(RuntimeError, match='Historical table disappeared'):
        driver.compare_history(before, after, {'adopted_instances': []})


def test_compare_history_0013_to_0014_rejects_changed_operations():
    before = {
        'schema': ['0013_technical_ownership'],
        'tables': {
            'technical_ownership': {'count': 0, 'sha256': 'before'},
            'operations': {'count': 3, 'sha256': 'operations'},
        },
        'technical_ownership_entries': {},
    }
    after = {
        'schema': [driver.HEAD],
        'tables': {
            'technical_ownership': {'count': 0, 'sha256': 'after'},
            'operations': {'count': 4, 'sha256': 'operations'},
        },
        'technical_ownership_entries': {},
    }
    with pytest.raises(RuntimeError, match='Historical rows changed: operations'):
        driver.compare_history(before, after, {'adopted_instances': []})


def test_compare_history_0013_to_0014_rejects_unexpected_table():
    before = {
        'schema': ['0013_technical_ownership'],
        'tables': {'technical_ownership': {'count': 0, 'sha256': 'before'}},
        'technical_ownership_entries': {},
    }
    after = {
        'schema': [driver.HEAD],
        'tables': {
            'technical_ownership': {'count': 0, 'sha256': 'after'},
            'unexpected': {'count': 1, 'sha256': 'unexpected'},
        },
        'technical_ownership_entries': {},
    }
    with pytest.raises(RuntimeError, match='Unexpected historical table'):
        driver.compare_history(before, after, {'adopted_instances': []})


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_require_tenant_authentication(client, auth, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
    response = client.get(path, headers=auth)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    password = auth["Authorization"].removeprefix("Bearer ")
    basic = base64.b64encode(("iacp:" + password).encode()).decode()
    assert client.get(path, headers={"Authorization": "Basic " + basic}).status_code == 200
    assert client.get("/v1/instances", headers={"Authorization": "Basic " + basic}).status_code == 401


def test_drain_blocks_all_mutations_without_queue_or_db_writes(client, auth, sessions, monkeypatch):
    monkeypatch.setenv("IACP_ADMISSION_ENABLED", "0")
    for path in ("/v1/jobs/smoke", "/v1/instances", "/v1/instances/missing/power/start"):
        response = client.post(path, headers=auth, json={})
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"
    assert client.get("/v1/instances", headers=auth).status_code == 200
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0
        assert session.scalar(select(func.count()).select_from(Operation)) == 0


def test_request_id_cannot_be_spoofed_and_logs_do_not_contain_credentials(client, auth, caplog):
    with caplog.at_level("INFO", logger="uvicorn.error"):
        response = client.get("/v1/instances?secret=do-not-log", headers={**auth, "X-Request-ID": "spoofed"})
    assert response.headers["X-Request-ID"] != "spoofed"
    assert auth["Authorization"] not in caplog.text
    assert "do-not-log" not in caplog.text
    event = json.loads(caplog.records[-1].message)
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["route"] == "/v1/instances"


def test_idle_and_history_guards_preserve_uncertainty():
    original = {"nonterminal": {}, "queues": {}, "unacked": 0,
                "tables": {"terraform_attempts": {"count": 4, "sha256": "original"}}}
    driver.check_idle(original)
    for changed in ({"nonterminal": {"operations": [{"status": "uncertain"}]}},
                    {"queues": {"lifecycle": 1}}, {"unacked": 1}):
        with pytest.raises(RuntimeError):
            driver.check_idle({**original, **changed})
    with pytest.raises(RuntimeError, match="Historical rows changed"):
        driver.compare_history(original, {"tables": {"terraform_attempts": {"count": 4, "sha256": "rewritten"}}})


def test_queued_guest_recovery_scope_allows_only_declared_work_or_terminal_retry():
    request = {
        "operation_id": "11111111-1111-1111-1111-111111111111",
        "job_id": "22222222-2222-2222-2222-222222222222",
    }
    queued = {
        "schema": [driver.HEAD],
        "nonterminal": {
            "guest_initializations": [{"id": request["operation_id"], "status": "queued"}],
            "operations": [{"id": request["operation_id"], "status": "queued"}],
            "jobs": [{"id": request["job_id"], "status": "queued"}],
        },
        "queues": {}, "unacked": 0,
    }
    driver.check_queued_guest_recovery_scope(queued, request)
    driver.check_queued_guest_recovery_scope({**queued, "nonterminal": {}}, request)
    with pytest.raises(RuntimeError, match="Unexpected nonterminal work"):
        driver.check_queued_guest_recovery_scope({**queued, "nonterminal": {
            "power_executions": [{"id": "other", "status": "running"}],
        }}, request)


def test_core_contract_can_enable_delete_without_lab_allowlist(tmp_path):
    env = tmp_path / "server.env"
    env.write_text("IACP_ADMISSION_ENABLED=0\nIACP_KVM_LIFECYCLE_ENABLED=0\n")
    promotion = object.__new__(driver.Promotion)
    promotion.m = {"environment_file": str(env)}
    promotion.creation_contract = lambda: "provisioning-api-core"
    promotion.evidence = lambda *args: None
    promotion.flags(admission=1, lifecycle=1, deletion=1)
    values = dict(line.split("=", 1) for line in env.read_text().splitlines())
    assert values == {
        "IACP_ADMISSION_ENABLED": "1",
        "IACP_KVM_LIFECYCLE_ENABLED": "1",
        "IACP_TERRAFORM_APPLY_ENABLED": "1",
        "IACP_TERRAFORM_DELETE_ENABLED": "1",
    }


@pytest.mark.parametrize("action", ["promote", "publish", "activate", "activate_core", "first_start", "rollback", "certify"])
def test_expired_change_cannot_mutate_runtime(tmp_path, action):
    change = tmp_path / "change.json"
    change.write_text(json.dumps({"window_start": "2020-01-01T00:00:00Z", "window_end": "2020-01-02T00:00:00Z"}))
    promotion = object.__new__(driver.Promotion)
    promotion.m = {"change_record": {"path": str(change), "sha256": driver.digest(change)}}
    commands = []
    promotion.compose = lambda *args, **kwargs: commands.append(args)
    with pytest.raises(RuntimeError, match="Outside authorized change window"):
        getattr(promotion, action)()
    assert commands == []


def test_external_probe_rejects_private_dns_before_reading_tokens(monkeypatch, tmp_path):
    from argparse import Namespace
    probe = load("public_probe", "smoke_public.py")
    monkeypatch.setattr(probe.socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("192.168.1.50", 443))])
    report = probe.probe(Namespace(vantage_id="unit-fixture", external_vantage=True, expected_public_ip=["192.168.1.50"]))
    assert report["passed"] is False
    assert report["checks"] == [{"name": "public_dns", "passed": False}]


def test_platform_smoke_does_not_require_deleted_default_instance(monkeypatch, tmp_path):
    probe = load("platform_smoke", "smoke_public.py")

    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class FakeTLS:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def getpeercert(self): return {"notAfter": "Jan 01 00:00:00 2035 GMT"}

    class FakeContext:
        def wrap_socket(self, transport, server_hostname): return FakeTLS()

    token = tmp_path / "token"
    cookie = tmp_path / "cookie"
    token.write_text("token")
    cookie.write_text("cookie")
    token.chmod(0o600)
    cookie.chmod(0o600)
    contract = {"paths": {path: {"get": {"security": [{"bearer": []}]}}
                           for path in probe.PATHS}}

    def fake_request(path, **kwargs):
        if kwargs.get("base", "").startswith("http://"):
            return 301, {"Location": probe.BASE + "/docs"}, b""
        if not kwargs.get("token"):
            return 401, {}, b""
        if path == "/openapi.json":
            return 200, {"X-Request-ID": "request"}, json.dumps(contract).encode()
        return 200, {"X-Request-ID": "request"}, b"[]"

    monkeypatch.setattr(probe, "resolved_addresses", lambda: ["8.8.8.8"])
    monkeypatch.setattr(probe.socket, "create_connection", lambda *args, **kwargs: FakeSocket())
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: FakeContext())
    monkeypatch.setattr(probe.ssl, "cert_time_to_seconds", lambda value: 4_000_000_000)
    monkeypatch.setattr(probe, "request", fake_request)
    args = SimpleNamespace(
        vantage_id="unit-fixture", external_vantage=True, expected_public_ip=["8.8.8.8"],
        manifest=None, lan_vpn_vantage=False, token_file=token,
        gateway_cookie_file=cookie, other_tenant_token_file=None,
        instance_id=None, operation_id=None, ca_file=None, min_validity_days=14,
    )
    report = probe.probe(args)
    assert report["passed"] is True
    assert report["instance_smoke"] == {
        "status": "not_applicable",
        "reason": "No se proporcionó una instancia IACP activa.",
    }
    assert not any("a1b2d063" in check["name"] for check in report["checks"])


def test_instance_smoke_checks_energy_when_instance_is_supplied(monkeypatch, tmp_path):
    probe = load("instance_smoke", "smoke_public.py")
    token = tmp_path / "token"
    cookie = tmp_path / "cookie"
    token.write_text("token")
    cookie.write_text("cookie")
    token.chmod(0o600)
    cookie.chmod(0o600)
    contract = {"paths": {path: {"get": {"security": [{"bearer": []}]}}
                           for path in probe.PATHS}}

    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class FakeTLS:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def getpeercert(self): return {"notAfter": "Jan 01 00:00:00 2035 GMT"}

    class FakeContext:
        def wrap_socket(self, transport, server_hostname): return FakeTLS()

    instance_id = "11111111-1111-4111-8111-111111111111"

    def fake_request(path, **kwargs):
        if kwargs.get("base", "").startswith("http://"):
            return 301, {"Location": probe.BASE + "/docs"}, b""
        if not kwargs.get("token"):
            return 401, {}, b""
        if path == "/openapi.json":
            return 200, {"X-Request-ID": "request"}, json.dumps(contract).encode()
        if path.endswith("/power"):
            return 200, {"X-Request-ID": "request"}, json.dumps({"guest_ready": None}).encode()
        return 200, {"X-Request-ID": "request"}, b"[]"

    monkeypatch.setattr(probe, "resolved_addresses", lambda: ["8.8.8.8"])
    monkeypatch.setattr(probe.socket, "create_connection", lambda *args, **kwargs: FakeSocket())
    monkeypatch.setattr(probe.ssl, "create_default_context", lambda **kwargs: FakeContext())
    monkeypatch.setattr(probe.ssl, "cert_time_to_seconds", lambda value: 4_000_000_000)
    monkeypatch.setattr(probe, "request", fake_request)
    args = SimpleNamespace(
        vantage_id="unit-fixture", external_vantage=True, expected_public_ip=["8.8.8.8"],
        manifest=None, lan_vpn_vantage=False, token_file=token,
        gateway_cookie_file=cookie, other_tenant_token_file=None,
        instance_id=instance_id, operation_id=None, ca_file=None, min_validity_days=14,
    )
    report = probe.probe(args)
    assert report["passed"] is True
    assert report["instance_smoke"] == {"status": "passed", "instance_id": instance_id}
    assert any(check["name"].endswith("/power") for check in report["checks"])


def test_public_router_keeps_crm_away_from_docs_and_admin_paths():
    policy = {"lan_cidrs": ["192.0.2.0/24"], "crm_source_cidrs": ["198.51.100.1/32"],
              "crm_origins": ["https://crm.fixture.invalid"], "admin_forward_auth_url": "https://identity.fixture.invalid/verify"}
    config = tls.build_public(policy)["http"]
    admin = config["routers"]["iacp-admin"]
    crm = config["routers"]["iacp-crm"]
    assert "corporate-identity" in admin["middlewares"]
    assert "corporate-identity" not in crm["middlewares"]
    assert "ClientIP(`198.51.100.1/32`)" in crm["rule"]
    assert "/docs" not in crm["rule"] and "/terraform" not in crm["rule"] and "/provisioning" not in crm["rule"]
    auth = config["middlewares"]["corporate-identity"]["forwardAuth"]
    assert auth["authRequestHeaders"] == ["Cookie"]
    assert auth["trustForwardHeader"] is False
    assert auth["tls"]["insecureSkipVerify"] is False
    for url in ("http://identity.invalid", "https://iac.cyberfuel.com/auth", "https://user:secret@identity.invalid"):
        with pytest.raises(ValueError):
            tls.build_public({**policy, "admin_forward_auth_url": url})


def test_root_transport_install_preserves_all_other_proxy_controls(tmp_path):
    from copy import deepcopy
    from unittest.mock import Mock
    policy={'lan_cidrs':['192.0.2.0/24'],'crm_source_cidrs':['198.51.100.1/32'],
        'crm_origins':['https://crm.fixture.invalid'],'admin_forward_auth_url':'https://identity.fixture.invalid/verify'}
    desired=tls.build_public(policy)
    original=deepcopy(desired)
    for name in ('iacp-admin','iacp-crm'):
        original['http']['routers'][name]['middlewares'].remove('root-tls-transport')
    original['http']['routers']['diagnostic'].pop('middlewares')
    for name in ('root-tls-transport','strip-root-transport'):
        original['http']['middlewares'].pop(name)
    active=tmp_path/'active.json';candidate=tmp_path/'candidate.json'
    active.write_text(json.dumps(original));candidate.write_text(json.dumps(desired))
    p=object.__new__(driver.Promotion);p.approve=Mock();p.proxy_exposure=Mock();p.compose=Mock(return_value='{}');p.evidence=Mock()
    p.m={'tls_dynamic_file':{'path':str(active),'sha256':driver.digest(active)},
        'root_transport_configuration':{'path':str(candidate),'sha256':driver.digest(candidate)}}
    malicious=deepcopy(desired);malicious['http']['middlewares']['authorized-sources']['ipAllowList']['sourceRange']=['0.0.0.0/0']
    candidate.write_text(json.dumps(malicious));p.m['root_transport_configuration']['sha256']=driver.digest(candidate)
    with pytest.raises(RuntimeError,match='outside the assertion'):
        p.attest_root_transport()
    assert json.loads(active.read_text())==original
    candidate.write_text(json.dumps(desired));p.m['root_transport_configuration']['sha256']=driver.digest(candidate)
    p.attest_root_transport()
    assert json.loads(active.read_text())==desired
    assert tls.root_tls_transport(desired)==desired
