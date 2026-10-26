"""Excepción comercial LAB persistente, acotada y sin eliminar barreras técnicas."""
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from iacp.models import BOOTSTRAP_TENANT_ID
from iacp.security import Principal
from iacp.services import commercial
from iacp.services.instances import Conflict
from tests.test_commercial_admission import commercial_config, store, specification
from tests.test_commercial_catalog import catalog_profile


def enable():
    path = Path(os.environ['IACP_COMMERCIAL_LIMITS_FILE'])
    raw = json.loads(path.read_text())
    row = raw['tenants'][BOOTSTRAP_TENANT_ID]
    row.update(lab_mng_physical_capacity_only=True,
               allowed_plans=['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4'])
    path.write_text(json.dumps(raw))
    return commercial.limits_for(BOOTSTRAP_TENANT_ID)


def test_exception_survives_reload_and_skips_only_commercial_budget(commercial_config, monkeypatch):
    limits = enable()
    used = dict(instances=10000, vcpus=10000, memory_mb=1000000, disk_gb=1000000,
                concurrent_operations=0)
    monkeypatch.setattr(commercial, 'usage', lambda *_: used)
    commercial.quota(None, BOOTSTRAP_TENANT_ID, limits,
                     dict(vcpus=8, memory_mb=16384, disk_gb=400))
    assert commercial.limits_for(BOOTSTRAP_TENANT_ID) == limits
    used['concurrent_operations'] = 1
    with pytest.raises(Conflict, match='QUOTA_EXCEEDED:concurrent_operations'):
        commercial.quota(None, BOOTSTRAP_TENANT_ID, limits)


def test_existing_quotas_unchanged_without_explicit_exception(commercial_config, monkeypatch):
    limits = commercial.limits_for(BOOTSTRAP_TENANT_ID)
    monkeypatch.setattr(commercial, 'usage', lambda *_: dict(
        instances=2, vcpus=0, memory_mb=0, disk_gb=0, concurrent_operations=0))
    for tenant in [BOOTSTRAP_TENANT_ID, 'another-tenant']:
        with pytest.raises(Conflict, match='QUOTA_EXCEEDED:instances'):
            commercial.quota(None, tenant, limits, dict(vcpus=1, memory_mb=2048, disk_gb=50))


@pytest.mark.parametrize('change', ['tenant', 'node', 'allowed_nodes', 'storage', 'profile'])
def test_exception_rejects_scope_change(commercial_config, change):
    limits = enable()
    tenant = BOOTSTRAP_TENANT_ID
    if change == 'tenant': tenant = '0294373d-7075-4eb5-9dca-9ff256298828'
    else:
        raw = json.loads(commercial_config.read_text())
        settings = raw['profiles']['lab-dhcp']['settings']
        if change == 'node': settings['node_name'] = 'production'
        if change == 'allowed_nodes': settings['allowed_nodes'].append('production')
        if change == 'storage': settings['boot_disk']['datastore_id'] = 'production'
        if change == 'profile': raw['profiles'].pop('lab-dhcp')
        commercial_config.write_text(json.dumps(raw))
    with pytest.raises(Conflict, match='EXCEPTION_SCOPE_INVALID'):
        commercial.quota(None, tenant, limits)


@pytest.mark.parametrize('plan', ['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4'])
def test_minimal_plans_reach_real_capacity_barrier(sessions, commercial_config, store, plan):
    enable()
    body = specification(plan).model_dump()
    body.pop('network'); body.pop('commercial_context')
    from iacp.commercial_contract import CommercialInstanceCreate
    spec = CommercialInstanceCreate.model_validate(body)
    capacity = Mock(side_effect=Conflict('COMMERCIAL_PHYSICAL_CAPACITY_EXCEEDED'))
    publish = Mock()
    with sessions() as session:
        with pytest.raises(Conflict, match='PHYSICAL_CAPACITY_EXCEEDED'):
            commercial.create(session, Principal(BOOTSTRAP_TENANT_ID, 'lab-swagger-postman', 'operator'),
                              spec, 'capacity-'+plan, publish, store, capacity=capacity)
    assert capacity.call_count == 1
    publish.assert_not_called()
    assert not store.redis.values


def test_versioned_configuration_explicitly_scoped():
    raw = json.loads(Path('docs/deployment/commercial-limits.lab-mng.json').read_text())
    assert set(raw['tenants']) == {BOOTSTRAP_TENANT_ID}
    row = commercial.Limits.model_validate(raw['tenants'][BOOTSTRAP_TENANT_ID])
    assert row.lab_mng_physical_capacity_only and row.max_concurrent_operations == 1
    assert row.allowed_plans == ['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4']


@pytest.mark.parametrize('plan', ['kvm-1', 'kvm-2', 'kvm-3', 'kvm-4'])
def test_minimal_creation_keeps_idempotency_and_exact_plan(sessions, commercial_config, store, plan):
    enable()
    from iacp.catalog import resolve_plan
    from iacp.commercial_contract import CommercialInstanceCreate
    from iacp.models import Instance
    body = specification(plan).model_dump()
    body.pop('network'); body.pop('commercial_context')
    spec = CommercialInstanceCreate.model_validate(body)
    capacity, publish = Mock(), Mock()
    principal = Principal(BOOTSTRAP_TENANT_ID, 'lab-swagger-postman', 'operator')
    with sessions() as session:
        accepted = commercial.create(session, principal, spec, 'minimal-'+plan, publish, store, capacity=capacity)
        replay = commercial.create(session, principal, spec, 'minimal-'+plan, publish, store, capacity=capacity)
        assert accepted == replay
        instance = session.get(Instance, accepted.instance_id)
        expected = resolve_plan(plan)
        assert (instance.vcpus, instance.memory_mb, instance.disk_gb) == (
            expected.vcpus, expected.memory_mb, expected.disk_gb)
        assert instance.commercial_context is None and instance.network_profile == 'lab-dhcp'
    assert capacity.call_count == publish.call_count == 1
