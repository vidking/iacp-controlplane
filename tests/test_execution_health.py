"""La salud efectiva del worker impide consumir autorizaciones inseguras."""
from unittest.mock import Mock

import pytest

from iacp.lifecycle.proxmox import PowerError, ProxmoxPower
from iacp.terraform.config import PlanError
from iacp.terraform.execution_health import require_execution_health
from iacp.terraform.executor import TerraformExecutor
from tests.test_clone_permissions import env, target


@pytest.fixture
def health_api(monkeypatch):
    values = {'storage': {'active': 1, 'enabled': 1, 'type': 'rbd'},
              'ceph': {'health': {'status': 'HEALTH_OK'}}}
    calls = []

    def read(self, method, path, parameters=None):
        assert method == 'GET'
        calls.append(path)
        value = values['ceph' if '/ceph/' in path else 'storage']
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ProxmoxPower, '_request', read)
    return values, calls


@pytest.mark.parametrize('state', ['HEALTH_WARN', 'HEALTH_ERR', None, ''])
def test_ceph_requires_exact_health_ok(target, health_api, state):
    values, calls = health_api
    values['ceph'] = {'health': {'status': state}}
    with pytest.raises(PlanError, match='EXECUTION_CEPH_NOT_HEALTHY'):
        require_execution_health(env(), target[1])
    assert calls[-1].endswith('/ceph/status')


@pytest.mark.parametrize('failure', [PowerError('POWER_PERMISSION_DENIED'),
    PowerError('POWER_PROXMOX_UNAVAILABLE'), TimeoutError(), {'health': None}])
def test_denied_missing_or_unreadable_health_fails_closed(target, health_api, failure):
    health_api[0]['ceph'] = failure
    with pytest.raises(PlanError, match='EXECUTION_HEALTH_UNAVAILABLE') as captured:
        require_execution_health(env(), target[1])
    assert 'synthetic' not in str(captured.value)


@pytest.mark.parametrize('storage', [{'active': 0, 'type': 'rbd'},
    {'active': 1, 'enabled': 0, 'type': 'rbd'}, {'active': 1}, None])
def test_storage_is_also_required(target, health_api, storage):
    health_api[0]['storage'] = storage
    with pytest.raises(PlanError, match='EXECUTION_STORAGE_UNAVAILABLE'):
        require_execution_health(env(), target[1])


def test_reads_all_stores_then_ceph_once_and_supports_non_ceph(target, health_api):
    expected = dict(target[1], clone_datastore_id='second-store')
    require_execution_health(env(), expected)
    assert len(health_api[1]) == 3
    health_api[0]['storage']['type'] = 'dir'
    health_api[1].clear()
    require_execution_health(env(), expected)
    assert len(health_api[1]) == 2
    assert all('/storage/' in path for path in health_api[1])


def test_cancelled_health_never_reads_proxmox(target, health_api):
    with pytest.raises(PlanError, match='CANCELLED'):
        require_execution_health(env(), target[1], lambda: True)
    assert health_api[1] == []


@pytest.mark.parametrize('healthy', [False, True])
def test_health_is_rechecked_after_plan_verification_before_apply(
        tmp_path, monkeypatch, target, health_api, healthy):
    import iacp.terraform.executor as module
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED', '1')
    executor = TerraformExecutor(tmp_path/'source', tmp_path/'artifacts', runner=Mock())
    monkeypatch.setattr(executor, 'prepare_apply', Mock())
    monkeypatch.setattr(executor, 'environment', lambda _: env())
    monkeypatch.setattr(executor, 'initialize', Mock())
    monkeypatch.setattr(executor, 'read_json', lambda *args: {})
    monkeypatch.setattr(executor, 'observe', lambda *args: {'present': True})
    monkeypatch.setattr(module, 'require_clone_permissions', Mock())

    def inspect(*args):
        # El plan se obtuvo sano; una degradación posterior debe detener apply.
        health_api[0]['ceph']['health']['status'] = 'HEALTH_OK' if healthy else 'HEALTH_ERR'
    monkeypatch.setattr(module, 'verify_plan', inspect)
    before = Mock()
    args = ('00000000-0000-4000-8000-000000000001', 1, 'a'*64,
            'owned/key', target[0].model_dump(), {'instance': target[1]})
    if healthy:
        executor.apply(*args, before_mutation=before)
        before.assert_called_once()
        executor.runner.apply_saved.assert_called_once()
    else:
        with pytest.raises(PlanError, match='EXECUTION_CEPH_NOT_HEALTHY'):
            executor.apply(*args, before_mutation=before)
        before.assert_not_called()
        executor.runner.apply_saved.assert_not_called()


def test_bad_health_stops_plan_before_workspace_creation(tmp_path, monkeypatch, target, health_api):
    import iacp.terraform.executor as module
    monkeypatch.setenv('IACP_TERRAFORM_PLAN_ENABLED', '1')
    executor = TerraformExecutor(tmp_path/'source', tmp_path/'artifacts', runner=Mock())
    monkeypatch.setattr(executor, 'environment', lambda _: env())
    monkeypatch.setattr(module, 'require_clone_permissions', Mock())
    health_api[0]['ceph']['health']['status'] = 'HEALTH_ERR'
    with pytest.raises(PlanError, match='EXECUTION_CEPH_NOT_HEALTHY'):
        executor.plan('00000000-0000-4000-8000-000000000001', 1, 'owned/key',
                      target[0], {'instance': target[1]}, lambda: False)
    assert not executor.artifacts.exists()
    executor.runner.run.assert_not_called()
