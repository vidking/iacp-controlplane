import base64
import importlib.util
import json
from pathlib import Path

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


def test_compare_history_0013_to_0014_allows_only_reported_ownership_rows():
    before = {
        'schema': ['0013_technical_ownership'],
        'tables': {
            'technical_ownership': {'count': 1, 'sha256': 'before'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
        },
        'technical_ownership_entries': {'existing': 'digest-existing'},
    }
    after = {
        'schema': [driver.HEAD],
        'tables': {
            'technical_ownership': {'count': 2, 'sha256': 'after'},
            'deletion_executions': {'count': 2, 'sha256': 'deletions'},
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
    with pytest.raises(RuntimeError, match='Historical rows changed: deletion_executions'):
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


@pytest.mark.parametrize("action", ["promote", "publish", "activate", "first_start", "rollback", "certify"])
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
