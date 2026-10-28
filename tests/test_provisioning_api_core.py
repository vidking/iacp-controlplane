"""Regresiones del contrato técnico sin decisiones comerciales."""
import json
from copy import deepcopy
from types import SimpleNamespace

from sqlalchemy import select

from iacp.commercial_contract import CommercialInstanceCreate, MINIMAL_EXAMPLE
from iacp.models import BOOTSTRAP_TENANT_ID, CommercialIntent, Instance, OperationEvent
from iacp.security import Principal
from iacp.services import commercial
from iacp.services.catalog import authorized_profile
from tests.test_commercial_admission import commercial_config, store
from tests.test_commercial_catalog import catalog_profile


def core_spec(**changes):
    body=deepcopy(MINIMAL_EXAMPLE)
    body['access']['root_password']='SENUELO-CORE-NO-PERSISTIR-2026'
    body.update(changes)
    return CommercialInstanceCreate.model_validate(body)


def test_core_create_uses_identity_and_technical_capacity_only(
        sessions, commercial_config, store, monkeypatch):
    monkeypatch.setenv('IACP_CREATION_CONTRACT', 'provisioning-api-core')
    published=[]
    with sessions() as session:
        accepted=commercial.create(session,
            Principal(BOOTSTRAP_TENANT_ID, 'lab-swagger-postman', 'operator'),
            core_spec(), 'core-create', published.append, store, capacity=lambda *_: None)
        instance=session.get(Instance, accepted.instance_id)
        intent=session.get(CommercialIntent, accepted.instance_id)
        events=session.scalars(select(OperationEvent).where(
            OperationEvent.operation_id == accepted.operation_id)).all()
        assert instance.commercial_revision == 'provisioning-api-core'
        assert instance.tenant_id == BOOTSTRAP_TENANT_ID
        assert intent.revision == 'provisioning-api-core'
        assert any(event.code == 'provisioning_accepted' for event in events)
        assert instance.commercial_context is None
    assert len(published) == 1


def test_core_catalog_resolves_all_authorized_golden_images_without_limits(
        commercial_config, monkeypatch):
    monkeypatch.setenv('IACP_CREATION_CONTRACT', 'provisioning-api-core')
    raw=json.loads(commercial_config.read_text())
    raw['lab_images']={key:{'enabled':True,'cloudinit_username':'root'}
        for key in ['ubuntu-2604','almalinux-9','almalinux-10']}
    commercial_config.write_text(json.dumps(raw))
    for image_id in ['ubuntu-2404','ubuntu-2604','almalinux-9','almalinux-10']:
        assert authorized_profile(BOOTSTRAP_TENANT_ID, image_id) is not None


def test_core_lifecycle_delegation_does_not_consult_commercial_limits(monkeypatch):
    monkeypatch.setattr(commercial, 'member', lambda *args, **kwargs: object())
    instance = SimpleNamespace(commercial_revision='provisioning-api-core', tenant_id=BOOTSTRAP_TENANT_ID)
    commercial.authorize_operation(None, instance, 'start')


def test_core_mutations_never_require_lab_policy_administration(client, auth, monkeypatch):
    monkeypatch.setenv('IACP_CREATION_CONTRACT', 'provisioning-api-core')
    policy = {
        'profiles': ['lab-dhcp'],
        'actions': ['create', 'start', 'shutdown', 'reboot', 'delete'],
        'max_instances': 1,
        'max_vcpus': 1,
        'max_memory_mb': 2048,
        'max_disk_gb': 50,
        'max_concurrent_operations': 1,
        'valid_for_seconds': 3600,
        'change_reference': 'CORE-NO-LAB-POLICY',
    }
    response = client.post('/v1/lab/policies', headers={**auth, 'Idempotency-Key': 'core-policy'}, json=policy)
    assert response.status_code == 404
    assert client.get('/v1/lab/policies', headers=auth).status_code == 404


def test_core_authorization_is_bound_to_persisted_contract_not_release_policy(monkeypatch):
    monkeypatch.setenv('IACP_CREATION_CONTRACT', 'historical')
    row = SimpleNamespace(revision='provisioning-api-core')
    monkeypatch.setattr(commercial, 'member', lambda *args, **kwargs: row)
    commercial.authorize_operation(
        None,
        SimpleNamespace(commercial_revision='provisioning-api-core', tenant_id=BOOTSTRAP_TENANT_ID),
        'delete',
    )


def test_core_uses_explicit_automation_identity():
    from iacp.services.automation import CORE_AUTOMATION_SUBJECT, technical_principal

    principal = technical_principal(BOOTSTRAP_TENANT_ID, 'provisioning-api-core')
    assert principal.subject == CORE_AUTOMATION_SUBJECT
    assert principal.role == 'automation'
    assert principal.administration is True


def test_core_public_errors_are_technical_codes(monkeypatch):
    monkeypatch.setenv('IACP_CREATION_CONTRACT', 'provisioning-api-core')
    from iacp.api.presentation import public_code
    assert public_code('COMMERCIAL_OWNERSHIP_BINDING_CHANGED') == 'PROVISIONING_OWNERSHIP_BINDING_CHANGED'
    assert public_code('POWER_INSTANCE_NOT_AUTHORIZED') == 'POWER_INSTANCE_NOT_AUTHORIZED'
