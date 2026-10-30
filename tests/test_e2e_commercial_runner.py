"""Valida la sonda preparada sin acceder a Proxmox ni ejecutar aprovisionamiento."""
import ast
import importlib.util
from pathlib import Path

import pytest


def runner():
    path=Path(__file__).resolve().parents[1]/'scripts/e2e-commercial-sequential.py'
    spec=importlib.util.spec_from_file_location('e2e_comercial_preparado',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_guest_probe_compiles_both_layers_and_supports_ext4_xfs():
    module=runner()
    tree=ast.parse(module.PROBE)
    guest=next(ast.literal_eval(node.value) for node in ast.walk(tree)
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='program' for t in node.targets))
    compile(guest,'guest-probe','exec')
    assert "filesystem=='ext4'" in guest and "filesystem=='xfs'" in guest
    assert "'dumpe2fs'" in guest and "'xfs_info'" in guest


@pytest.mark.parametrize('change',['disk_bytes','root_partition_end_bytes','filesystem_raw_bytes'])
def test_storage_validation_rejects_incomplete_growth(change):
    module=runner();disk=50*1024**3
    guest=dict(disk_bytes=disk,root_partition_end_bytes=disk-1024,
               root_partition_bytes=disk-1024**2,filesystem_raw_bytes=disk-1024**2,
               filesystem_block_bytes=4096)
    module.validate_storage(guest,50)
    guest[change]-=1024**3
    with pytest.raises(AssertionError):module.validate_storage(guest,50)


def instance(instance_id,status='ready',vmid=30093,name='historica'):
    return {
        'instance_id':instance_id,'tenant_id':'tenant-lab','name':name,
        'image':'ubuntu-2404','vmid':vmid,'node':'mng01','vcpus':1,
        'memory_mb':2048,'disk_gb':50,'network_mode':'dhcp',
        'network_profile':'lab-dhcp','executor':'provisioning',
        'infrastructure_kind':'kvm','status':status,'operation_id':'op-'+instance_id,
        'job_id':'job-'+instance_id,'ip_addresses':[],'network_observation':None,
    }


def test_inventory_scope_accepts_historical_instances_and_deleted_test_instance():
    module=runner()
    historical=instance('historical-30093',status='ready',vmid=30093)
    created=instance('created-ubuntu',status='deleted',vmid=30100,name='e2e-ubuntu')
    before=[module._safe_instance_record(historical)]
    after=[module._safe_instance_record(historical),module._safe_instance_record(created)]

    diff=module.inventory_scope_diff(before,after,{'created-ubuntu'})

    assert diff['scope_ok'] is True
    assert diff['created_instance_ids']==['created-ubuntu']
    assert diff['missing_baseline_instance_ids']==[]
    assert diff['unexpected_instance_ids']==[]


def test_inventory_scope_rejects_untracked_resource_and_identity_change():
    module=runner()
    historical=instance('historical-30093',status='ready',vmid=30093)
    changed=instance('historical-30093',status='ready',vmid=39999)
    untracked=instance('untracked',status='ready',vmid=30101)

    diff=module.inventory_scope_diff(
        [module._safe_instance_record(historical)],
        [module._safe_instance_record(changed),module._safe_instance_record(untracked)],
        set(),
    )

    assert diff['scope_ok'] is False
    assert diff['unexpected_instance_ids']==['untracked']
    assert diff['baseline_identity_changes'][0]['instance_id']=='historical-30093'


def test_next_pilot_name_skips_tombstones_with_neutral_names():
    module=runner()
    reserved={'prueba-kvm-2404-001','prueba-kvm-2404-003'}

    assert module.next_pilot_name('ubuntu-2404', reserved)=='prueba-kvm-2404-002'
    reserved.add('prueba-kvm-2404-002')
    assert module.next_pilot_name('ubuntu-2404', reserved)=='prueba-kvm-2404-004'
    assert 'agent' not in module.next_pilot_name('ubuntu-2604', set())


def test_probe_accepts_completed_technical_access_without_commercial_bootstrap():
    module=runner()
    assert "technical_access = (i.access_verification or {}).get('status') == 'completed'" in module.PROBE
    assert "commercial_access = c.bootstrap_status == 'completed'" in module.PROBE
    assert "assert observation['present'] and resource" in module.PROBE


def test_runner_blocks_almalinux_and_preserves_global_uncertainty():
    module=runner()
    assert module.SUPPORTED_E2E_IMAGES=={'ubuntu-2404','ubuntu-2604'}
    assert module._operation_status({'status':'uncertain'})=='uncertain'
    assert 'allow_reconcile' in module.wait_operation.__code__.co_varnames


def test_fulfillment_accepts_certified_ready_contract_and_rejects_unverified_ready():
    module=runner()

    assert module.fulfillment_is_certified({
        'fulfillment': {'status': 'completed', 'phase': 'verified'},
    })
    assert module.fulfillment_is_certified({
        'fulfillment': {'status': 'completed', 'phase': 'ready', 'certified_ready': True},
    })
    assert not module.fulfillment_is_certified({
        'fulfillment': {'status': 'completed', 'phase': 'ready', 'certified_ready': False},
    })
    assert not module.fulfillment_is_certified({
        'fulfillment': {'status': 'running', 'phase': 'ready', 'certified_ready': True},
    })


def test_global_uncertainty_blocks_before_any_create(monkeypatch):
    module=runner()
    monkeypatch.setattr(module,'list_non_terminal_operations',lambda:[{
        'operation_id':'bootstrap-uncertain','instance_id':'historical-30093',
        'kind':'commercial_bootstrap','status':'uncertain',
        'normalized_status':'uncertain','error_code':'BOOTSTRAP_RECONCILIATION_REQUIRED',
        'recovery_required':True,
    }])

    with pytest.raises(RuntimeError,match='GLOBAL_NON_TERMINAL_OPERATION_BLOCKS_E2E'):
        module.assert_no_global_mutations()


def test_global_guard_allows_only_readonly_discovery_for_created_pilot(monkeypatch):
    module=runner()
    module.CREATED_INSTANCE_IDS={'pilot-instance'}
    monkeypatch.setattr(module,'list_non_terminal_operations',lambda:[{
        'operation_id':'discover-pilot','instance_id':'pilot-instance',
        'kind':'instance.network.discover','status':'running',
        'normalized_status':'running','error_code':None,
    }])
    assert module.assert_no_global_mutations()==[]

    monkeypatch.setattr(module,'list_non_terminal_operations',lambda:[{
        'operation_id':'discover-other','instance_id':'other-instance',
        'kind':'instance.network.discover','status':'running',
        'normalized_status':'running','error_code':None,
    }])
    with pytest.raises(RuntimeError,match='GLOBAL_NON_TERMINAL_OPERATION_BLOCKS_E2E'):
        module.assert_no_global_mutations()


@pytest.mark.parametrize('message',[
    'GLOBAL_NON_TERMINAL_OPERATION_BLOCKS_E2E:bootstrap-uncertain',
    'OPERATION_UNCERTAIN',
    'FULFILLMENT_FAILED',
    'INSTANCE_UNCERTAIN',
])
def test_guest_wait_does_not_swallow_terminal_or_uncertain_errors(message):
    module=runner()
    assert module._must_abort_guest_wait(RuntimeError(message))
    assert not module._must_abort_guest_wait(RuntimeError('WORKER_PROBE_FAILED'))
