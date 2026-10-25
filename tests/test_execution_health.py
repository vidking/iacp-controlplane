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
    values = {'storage': {'active': 1, 'enabled': 1, 'type': 'rbd',
              'avail': 1000*1024**3, 'total': 2000*1024**3, 'content': 'images'}, 'content': []}
    calls = []

    def read(self, method, path, parameters=None):
        assert method == 'GET'
        calls.append(path)
        assert '/ceph/' not in path
        value = values['content' if '/content?' in path else 'storage']
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(ProxmoxPower, '_request', read)
    return values, calls


def test_rbd_uses_only_target_backend_reads_without_global_ceph(target, health_api):
    require_execution_health(env(), target[1])
    assert len(health_api[1]) == 2
    assert all('/storage/' in path and '/ceph/' not in path for path in health_api[1])


@pytest.mark.parametrize('failure', [PowerError('POWER_PERMISSION_DENIED'),
    PowerError('POWER_PROXMOX_UNAVAILABLE'), TimeoutError()])
def test_unreadable_backend_fails_closed(target, health_api, failure):
    health_api[0]['content'] = failure
    with pytest.raises(PlanError, match='EXECUTION_HEALTH_UNAVAILABLE') as captured:
        require_execution_health(env(), target[1])
    assert 'synthetic' not in str(captured.value)


@pytest.mark.parametrize('field,value', [('active',0),('enabled',0),('type',None),
    ('avail',None),('avail',True),('avail',0),('total',None),('total',-1),('content','iso')])
def test_backend_status_must_be_available_and_verifiable(target, health_api, field, value):
    health_api[0]['storage'][field] = value
    with pytest.raises(PlanError, match='EXECUTION_STORAGE_UNAVAILABLE'):
        require_execution_health(env(), target[1])


@pytest.mark.parametrize('value', [None, {}, [None], [{'volid':'other:disk','content':'images'}],
    [{'volid':'fixture-storage:disk','content':'iso'}]])
def test_invalid_backend_listing_fails_closed(target, health_api, value):
    health_api[0]['content'] = value
    with pytest.raises(PlanError, match='EXECUTION_STORAGE_READ_FAILED'):
        require_execution_health(env(), target[1])


def test_disk_capacity_cannot_be_ignored(target, health_api):
    health_api[0]['storage']['avail'] = target[1]['boot_disk']['size_gb'] * 1024**3 - 1
    with pytest.raises(PlanError, match='EXECUTION_STORAGE_CAPACITY_EXCEEDED'):
        require_execution_health(env(), target[1])
    health_api[0]['storage']['avail'] += 1
    require_execution_health(env(), target[1])


def test_reads_each_target_store_for_ceph_and_non_ceph(target, health_api):
    expected = dict(target[1], clone_datastore_id='second-store')
    require_execution_health(env(), expected)
    assert len(health_api[1]) == 4
    health_api[0]['storage']['type'] = 'dir'
    health_api[1].clear()
    require_execution_health(env(), expected)
    assert len(health_api[1]) == 4


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
        health_api[0]['storage']['active'] = 1 if healthy else 0
    monkeypatch.setattr(module, 'verify_plan', inspect)
    before = Mock()
    args = ('00000000-0000-4000-8000-000000000001', 1, 'a'*64,
            'owned/key', target[0].model_dump(), {'instance': target[1]})
    if healthy:
        executor.apply(*args, before_mutation=before)
        before.assert_called_once()
        executor.runner.apply_saved.assert_called_once()
    else:
        with pytest.raises(PlanError, match='EXECUTION_STORAGE_UNAVAILABLE'):
            executor.apply(*args, before_mutation=before)
        before.assert_not_called()
        executor.runner.apply_saved.assert_not_called()


def test_bad_health_stops_plan_before_workspace_creation(tmp_path, monkeypatch, target, health_api):
    import iacp.terraform.executor as module
    monkeypatch.setenv('IACP_TERRAFORM_PLAN_ENABLED', '1')
    executor = TerraformExecutor(tmp_path/'source', tmp_path/'artifacts', runner=Mock())
    monkeypatch.setattr(executor, 'environment', lambda _: env())
    monkeypatch.setattr(module, 'require_clone_permissions', Mock())
    health_api[0]['storage']['active'] = 0
    with pytest.raises(PlanError, match='EXECUTION_STORAGE_UNAVAILABLE'):
        executor.plan('00000000-0000-4000-8000-000000000001', 1, 'owned/key',
                      target[0], {'instance': target[1]}, lambda: False)
    assert not executor.artifacts.exists()
    executor.runner.run.assert_not_called()


@pytest.mark.parametrize('vmid',[104,29000,29001,29010,29011])
def test_protected_targets_never_reach_prepare_or_apply(tmp_path,monkeypatch,target,vmid):
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED','1')
    executor=TerraformExecutor(tmp_path/'source',tmp_path/'artifacts',runner=Mock())
    prepare=Mock();monkeypatch.setattr(executor,'prepare_apply',prepare)
    expected={**target[1],'vm_id':vmid}
    with pytest.raises(PlanError,match='PROTECTED_RESOURCE'):
        executor.apply('00000000-0000-4000-8000-000000000001',1,'a'*64,
            'owned/key',target[0].model_dump(),{'instance':expected})
    prepare.assert_not_called();executor.runner.apply_saved.assert_not_called()


def test_disposable_106_is_not_blocked_by_the_protected_target_guard(tmp_path,monkeypatch,target):
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED','1')
    executor=TerraformExecutor(tmp_path/'source',tmp_path/'artifacts',runner=Mock())
    prepare=Mock(side_effect=PlanError('PREPARE_REACHED'))
    monkeypatch.setattr(executor,'prepare_apply',prepare)
    with pytest.raises(PlanError,match='PREPARE_REACHED'):
        executor.apply('00000000-0000-4000-8000-000000000001',1,'a'*64,
            'owned/key',target[0].model_dump(),{'instance':{**target[1],'vm_id':106}})
    prepare.assert_called_once();executor.runner.apply_saved.assert_not_called()
