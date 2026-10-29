"""Provider/state fixtures plus real Terraform core locally; no enterprise resources."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from iacp.terraform import deletion_executor as module
from iacp.terraform.config import Allocation, PlanError, SOURCE_REVISION
from iacp.terraform.deletion_executor import (
    DeletionExecutor,
    allow_historical_delete_hardware,
    allow_historical_delete_plan_defaults,
)
from iacp.terraform.executor import ADDRESS, matches_sealed_variables
from iacp.terraform.runner import CommandRunner
from iacp.terraform.state import StateSnapshot
from tests.test_deletion import deletion_plan
from tests.test_terraform import allocation_data, plan_json


@pytest.fixture
def deletion_executor(tmp_path,monkeypatch):
    allocation=Allocation.model_validate(allocation_data(30000))
    instance=SimpleNamespace(name='disposable',image='test-image',disk_gb=20,vcpus=2,memory_mb=2048)
    ctx={'instance_id':str(uuid4()),'operation_id':str(uuid4()),'attempt':1,'provisioning_plan_sha256':'a'*64,
         'state_key':'synthetic-state','source_revision':SOURCE_REVISION,
         'allocation':allocation.model_dump(),'variables':allocation.variables(instance),
         'backend':allocation.backend.arguments('synthetic-state')}
    approved=plan_json(ctx['variables'])
    values=deepcopy(approved['resource_changes'][0]['change']['after']);values['id']='30000'
    destroy=deletion_plan();destroy['resource_changes'][0]['change']['before']=values
    destroy['prior_state']['values']['root_module']['resources'][0]['values']=values
    state={'version':4,'lineage':'synthetic-lineage','serial':1,'resources':[{'mode':'managed','module':'module.kvm',
        'type':'proxmox_virtual_environment_vm','name':'this','instances':[{'attributes':values}]}]}
    store=SimpleNamespace(state=state,version='v1',reader_calls=0,stopped=0,absent=False,plan=destroy)
    class Reader:
        def __init__(self,*args):pass
        def read(self,cancelled):
            cancelled();store.reader_calls+=1
            return StateSnapshot.parse(json.dumps(store.state).encode(),store.version)
        def close(self):pass
    class Runner:
        def __init__(self):self.calls=[]
        def run(self,args,cwd,env,cancelled,**kwargs):
            cancelled();self.calls.append(args)
            if args==['version','-json']:return json.dumps({'terraform_version':'1.16.6'}).encode()
            if args[0]=='show':return json.dumps(approved if args[-1].endswith('/saved.tfplan') else destroy).encode()
            return b''
        def plan_destroy(self,cwd,env,cancelled,**kwargs):
            cancelled();self.calls.append(['plan_destroy',kwargs]);(cwd/'deletion.tfplan').write_bytes(b'synthetic saved destroy plan')
        def apply_destroy(self,cwd,env,cancelled):
            cancelled();self.calls.append(['apply_destroy']);store.state['resources']=[];store.state['serial']=2;store.version='v2'
        def destroy(self,cwd,env,cancelled,**kwargs):
            cancelled();self.calls.append(['destroy',kwargs]);store.state['resources']=[];store.state['serial']=2;store.version='v2';store.absent=True
    runner=Runner();executor=DeletionExecutor(tmp_path/'source',tmp_path/'artifacts',runner)
    monkeypatch.setattr(executor,'verify_artifacts',lambda *args,**kwargs:None)
    monkeypatch.setattr(executor,'environment',lambda *args:{'PROXMOX_VE_ENDPOINT':'https://fixture.invalid','PROXMOX_VE_API_TOKEN':'synthetic-token'})
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as tar:
        data=b'terraform {}';item=tarfile.TarInfo('stacks/kvm-instance/main.tf');item.size=len(data);tar.addfile(item,io.BytesIO(data))
    monkeypatch.setattr(module.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout=archive.getvalue()))
    def stopped(*args):store.stopped+=1
    monkeypatch.setattr(module,'stopped',stopped)
    monkeypatch.setattr(module,'S3StateReader',Reader)
    monkeypatch.setattr(module,'proxmox_resource',lambda *args:{'present':not store.absent and store.state['serial']<2})
    return executor,ctx,store


def test_delete_compatibility_accepts_only_missing_optional_golden_vmtag():
    sealed = {
        'instance': {'vm_id': 30000, 'node_name': 'mng01'},
        'iacp_golden_hardware': {'base': 'golden', 'disk': {'size': 20}},
    }
    current = deepcopy(sealed)
    current['iacp_golden_hardware']['vmtag'] = None

    assert not matches_sealed_variables(sealed, current)
    assert matches_sealed_variables(sealed, current, allow_legacy_optional_golden_vmtag=True)

    changed = deepcopy(current)
    changed['iacp_golden_hardware']['vmtag'] = 'ubuntu-2404'
    assert not matches_sealed_variables(sealed, changed, allow_legacy_optional_golden_vmtag=True)

    changed = deepcopy(current)
    changed['instance']['vm_id'] = 30001
    assert not matches_sealed_variables(sealed, changed, allow_legacy_optional_golden_vmtag=True)


def test_delete_passes_legacy_golden_compatibility_only_to_artifact_gate(deletion_executor):
    executor,ctx,store=deletion_executor
    calls=[]
    executor.verify_artifacts=lambda *args, **kwargs: calls.append(kwargs)
    executor.plan_deletion(ctx,lambda:False)
    assert calls and all(call.get('allow_legacy_optional_golden_vmtag') is True for call in calls)


def test_saved_delete_plan_apply_and_readonly_reconciliation(deletion_executor):
    executor,ctx,store=deletion_executor
    plan=executor.plan_deletion(ctx,lambda:False);mutations=[]
    result=executor.apply_deletion(ctx,plan['plan_sha256'],lambda:False,lambda:mutations.append(True))
    assert mutations==[True] and result['deleted'] and store.stopped==4
    assert executor.runner.calls.count(['apply_destroy'])==1
    executor.runner.calls.clear()
    assert executor.reconcile_deletion(ctx,plan['plan_sha256'],lambda:False)['deleted']
    assert executor.runner.calls==[]


def test_tainted_recovery_uses_fixed_destroy_without_adopting_resource(deletion_executor):
    executor,ctx,store=deletion_executor
    store.state['resources'][0]['instances'][0]['status']='tainted'
    # Un plan de aprovisionamiento fallido puede no satisfacer la barrera de
    # hardware; la recuperación solo debe sellar y ejecutar el destroy.
    original = module.verify_plan
    module.verify_plan = lambda *args, **kwargs: (_ for _ in ()).throw(PlanError('PLAN_MISMATCH'))
    try:
        plan=executor.plan_deletion(ctx,lambda:False,allow_tainted=True)
    finally:
        module.verify_plan = original
    result=executor.apply_deletion(ctx,plan['plan_sha256'],lambda:False,lambda:None,allow_tainted=True)
    assert result['deleted']
    assert ['destroy', {'refresh': False}] in executor.runner.calls
    assert ['apply_destroy'] not in executor.runner.calls


@pytest.mark.parametrize(('source_revision','vmid','golden','expected'), [
    ('a' * 40, 30059, True, True),
    (SOURCE_REVISION, 30059, True, False),
    ('a' * 40, 106, True, True),
    ('a' * 40, 30059, False, False),
])
def test_historical_delete_hardware_compatibility_is_narrow(source_revision, vmid, golden, expected):
    context={'source_revision': source_revision,
             'variables': {'instance': {'vm_id': vmid},
                           **({'iacp_golden_hardware': {}} if golden else {})}}
    assert allow_historical_delete_hardware(context) is expected


def test_historical_delete_accepts_stable_provider_pool_default_only():
    allocation = Allocation.model_validate(allocation_data(106))
    instance = SimpleNamespace(name='historical', image='test-image', disk_gb=20, vcpus=2, memory_mb=2048)
    variables = allocation.variables(instance)
    context = {
        'source_revision': 'a' * 40,
        'variables': {'instance': {'vm_id': 106}, **variables},
    }
    approved = plan_json(variables)
    current = deepcopy(approved['resource_changes'][0]['change']['after'])
    current['pool_id'] = 'iacp-lab'

    assert allow_historical_delete_plan_defaults(context)
    from iacp.terraform.deletion_executor import verify_topology
    verify_topology(approved, current, allow_historical_pool_id_default=True)


@pytest.mark.parametrize('expected_pool_id', [None, ''])
def test_historical_delete_accepts_pool_materialized_from_empty_state(expected_pool_id):
    allocation = Allocation.model_validate(allocation_data(106))
    instance = SimpleNamespace(name='historical', image='test-image', disk_gb=20, vcpus=2, memory_mb=2048)
    variables = allocation.variables(instance)
    approved = plan_json(variables)
    approved['resource_changes'][0]['change']['after']['pool_id'] = expected_pool_id
    persisted = deepcopy(approved['resource_changes'][0]['change']['after'])
    persisted['pool_id'] = ''
    current = deepcopy(persisted)
    current['pool_id'] = 'iacp-lab'

    from iacp.terraform.deletion_executor import verify_topology
    verify_topology(
        approved,
        current,
        persisted=persisted,
        allow_historical_pool_id_default=True,
    )


def test_historical_delete_rejects_unrelated_pool_materialized_from_empty_state():
    allocation = Allocation.model_validate(allocation_data(106))
    instance = SimpleNamespace(name='historical', image='test-image', disk_gb=20, vcpus=2, memory_mb=2048)
    variables = allocation.variables(instance)
    approved = plan_json(variables)
    persisted = deepcopy(approved['resource_changes'][0]['change']['after'])
    persisted['pool_id'] = ''
    current = deepcopy(persisted)
    current['pool_id'] = 'other-pool'

    from iacp.terraform.deletion_executor import verify_topology
    with pytest.raises(PlanError, match='DELETE_TOPOLOGY_CHANGED'):
        verify_topology(
            approved,
            current,
            persisted=persisted,
            allow_historical_pool_id_default=True,
        )


def test_historical_delete_rejects_pool_change_after_first_observation():
    allocation = Allocation.model_validate(allocation_data(106))
    instance = SimpleNamespace(name='historical', image='test-image', disk_gb=20, vcpus=2, memory_mb=2048)
    variables = allocation.variables(instance)
    approved = plan_json(variables)
    persisted = deepcopy(approved['resource_changes'][0]['change']['after'])
    persisted['pool_id'] = 'iacp-lab'
    current = deepcopy(persisted)
    current['pool_id'] = 'other-pool'

    from iacp.terraform.deletion_executor import verify_topology
    with pytest.raises(PlanError, match='DELETE_TOPOLOGY_CHANGED'):
        verify_topology(
            approved,
            current,
            persisted=persisted,
            allow_historical_pool_id_default=True,
        )


def test_current_delete_does_not_relax_pool_default():
    allocation = Allocation.model_validate(allocation_data(30000))
    instance = SimpleNamespace(name='disposable', image='test-image', disk_gb=20, vcpus=2, memory_mb=2048)
    variables = allocation.variables(instance)
    approved = plan_json(variables)
    current = deepcopy(approved['resource_changes'][0]['change']['after'])
    current['pool_id'] = 'iacp-lab'

    from iacp.terraform.deletion_executor import verify_topology
    with pytest.raises(PlanError):
        verify_topology(approved, current)


@pytest.mark.parametrize('change',['binary','variables','context','credential','state-version','state-lineage','extra-state-resource'])
def test_delete_revalidates_all_bindings_before_mutation(deletion_executor,change):
    executor,ctx,store=deletion_executor;plan=executor.plan_deletion(ctx,lambda:False)
    root=executor.deletion_workspace(ctx['instance_id'],ctx['operation_id'])/'source/stacks/kvm-instance'
    if change=='binary':(root/'deletion.tfplan').write_bytes(b'tampered')
    elif change=='variables':(root/'iacp.auto.tfvars.json').write_text('{}')
    elif change=='context':ctx=deepcopy(ctx);ctx['state_key']='another-tenant'
    elif change=='credential':executor.environment=lambda *args:{'PROXMOX_VE_ENDPOINT':'https://other.invalid','PROXMOX_VE_API_TOKEN':'other'}
    elif change=='state-version':store.version='v2'
    elif change=='state-lineage':store.state['lineage']='other-lineage'
    else:store.state['resources'].append(deepcopy(store.state['resources'][0]))
    with pytest.raises(PlanError):executor.apply_deletion(ctx,plan['plan_sha256'],lambda:False,lambda:pytest.fail('must not mutate'))
    assert ['apply_destroy'] not in executor.runner.calls


@pytest.mark.parametrize('problem',['resource-present','pve-present','same-serial','lineage-changed','unversioned','backend-error'])
def test_recovery_requires_stable_versioned_absence_in_both_systems(deletion_executor,monkeypatch,problem):
    executor,ctx,store=deletion_executor;plan=executor.plan_deletion(ctx,lambda:False)
    store.state['serial']=2;store.version='v2'
    if problem!='resource-present':store.state['resources']=[]
    if problem=='pve-present':monkeypatch.setattr(module,'proxmox_resource',lambda *args:{'present':True})
    elif problem=='same-serial':store.state['serial']=1
    elif problem=='lineage-changed':store.state['lineage']='other'
    elif problem=='unversioned':store.version=None
    elif problem=='backend-error':
        class Reader:
            def __init__(self,*args):pass
            def read(self,*args):raise PlanError('STATE_BACKEND_READ_FAILED')
            def close(self):pass
        monkeypatch.setattr(module,'S3StateReader',Reader)
    executor.runner.calls.clear()
    with pytest.raises(PlanError):executor.reconcile_deletion(ctx,plan['plan_sha256'],lambda:False)
    assert executor.runner.calls==[]


def test_real_terraform_core_saved_destroy_uses_fixed_commands(tmp_path,monkeypatch):
    # Terraform's built-in provider, local scratch state; no Proxmox or Wasabi.
    monkeypatch.setenv('IACP_TERRAFORM_APPLY_ENABLED','1');monkeypatch.setenv('IACP_TERRAFORM_DELETE_ENABLED','1')
    (tmp_path/'main.tf').write_text('resource "terraform_data" "disposable" { input = "fixture" }')
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(tmp_path),'TF_INPUT':'0','CHECKPOINT_DISABLE':'1'}
    runner=CommandRunner(timeout=20)
    runner.run(['init','-backend=false','-input=false'],tmp_path,env,lambda:False)
    runner.run(['plan','-input=false','-out=saved.tfplan'],tmp_path,env,lambda:False)
    runner.apply_saved(tmp_path,env,lambda:False)
    runner.plan_destroy(tmp_path,env,lambda:False)
    plan=json.loads(runner.run(['show','-json','deletion.tfplan'],tmp_path,env,lambda:False))
    assert plan['resource_changes'][0]['change']['actions']==['delete']
    runner.apply_destroy(tmp_path,env,lambda:False)
    state=json.loads(runner.run(['state','pull'],tmp_path,env,lambda:False))
    assert state['resources']==[]


@pytest.mark.parametrize('case',['allowed','no-allocate','running','pending','ha'])
def test_delete_pve_preflight_uses_terraform_identity_and_never_posts(monkeypatch,case):
    from iacp.lifecycle.proxmox import PowerError
    calls=[]
    class Client:
        def _request(self,method,path):
            calls.append((method,path));return {'/vms/30000':{'VM.Audit':0,**({} if case=='no-allocate' else {'VM.Allocate':0})}}
        def observe(self,target,preflight=False):
            assert preflight
            if case in {'pending','ha'}:raise PowerError('POWER_PENDING_CONFIGURATION' if case=='pending' else 'POWER_HA_UNSUPPORTED')
            return {'power_state':'running' if case=='running' else 'stopped'}
    monkeypatch.setattr(module,'ProxmoxPower',lambda *args:Client())
    env={'PROXMOX_VE_ENDPOINT':'https://fixture.invalid','PROXMOX_VE_API_TOKEN':'synthetic'}
    args=(env,{'instance':{'vm_id':30000,'node_name':'mng01','vm_name':'disposable'}},lambda:False)
    if case=='allowed':module.stopped(*args)
    else:
        with pytest.raises(PlanError):module.stopped(*args)
    assert all(method=='GET' for method,path in calls)


@pytest.mark.parametrize('field',['disk','network_device','cpu','initialization'])
def test_external_resource_topology_changes_are_never_swept_into_delete(field):
    from iacp.terraform.deletion_executor import verify_topology
    variables=Allocation.model_validate(allocation_data(30000)).variables(SimpleNamespace(name='disposable',image='test-image',disk_gb=20,vcpus=2,memory_mb=2048))
    approved=plan_json(variables);current=deepcopy(approved['resource_changes'][0]['change']['after'])
    if field in {'disk','network_device'}:current[field].append(deepcopy(current[field][0]))
    elif field=='cpu':current['cpu'][0]['cores']=8
    else:current['initialization'][0]['ip_config'][0]['ipv4'][0]['address']='192.0.2.99/24'
    with pytest.raises(PlanError):verify_topology(approved,current)


def test_only_power_ownership_is_ignored_for_delete_not_provisioning():
    from iacp.terraform.deletion_executor import verify_topology
    variables=Allocation.model_validate(allocation_data(30000)).variables(SimpleNamespace(name='disposable',image='test-image',disk_gb=20,vcpus=2,memory_mb=2048))
    approved=plan_json(variables);original=deepcopy(approved);current=deepcopy(approved['resource_changes'][0]['change']['after'])
    current['started']=not current['started']
    verify_topology(approved,current)
    assert approved==original


@pytest.mark.parametrize('reappears',[False,True])
@pytest.mark.parametrize('drift_present',[False,True])
def test_external_deletion_uses_saved_refresh_without_repeating_provider_destroy(deletion_executor,monkeypatch,reappears,drift_present):
    executor,ctx,store=deletion_executor
    store.absent=True
    store.plan['variables']={k:{'value':v} for k,v in ctx['variables'].items()}
    original=deepcopy(store.plan['resource_changes'][0])
    store.plan['resource_drift']=[original] if drift_present else []
    store.plan['resource_changes']=[]
    store.plan['prior_state']={'values':{}}
    plan=executor.plan_deletion(ctx,lambda:False)
    workspace=executor.deletion_workspace(ctx['instance_id'],ctx['operation_id'])
    manifest=json.loads((workspace/'deletion-manifest.json').read_text())
    assert manifest['absence_only'] is True
    if reappears:
        store.absent=False
        with pytest.raises(PlanError,match='ABSENCE_CHANGED'):
            executor.apply_deletion(ctx,plan['plan_sha256'],lambda:False,lambda:pytest.fail('No consumir'))
        assert ['apply_destroy'] not in executor.runner.calls
    else:
        assert executor.apply_deletion(ctx,plan['plan_sha256'],lambda:False,lambda:None)['deleted']
        assert store.stopped==0 and store.state['serial']==2


@pytest.mark.parametrize('mutation',['foreign-vmid','wrong-variables','extra-resource','foreign-provider','creates'])
def test_external_absence_plan_rejects_unrelated_or_ambiguous_changes(mutation):
    variables=Allocation.model_validate(allocation_data(30000)).variables(SimpleNamespace(name='disposable',image='test-image',disk_gb=20,vcpus=2,memory_mb=2048))
    plan=deletion_plan();drift=plan.pop('resource_changes')
    drift[0]['change']['before']=plan_json(variables)['resource_changes'][0]['change']['after']
    plan.update(resource_drift=drift,resource_changes=[],prior_state={'values':{}},variables={k:{'value':v} for k,v in variables.items()})
    if mutation=='foreign-vmid':drift[0]['change']['before']['vm_id']=30001
    elif mutation=='wrong-variables':plan['variables']={}
    elif mutation=='extra-resource':plan['resource_drift'].append(deepcopy(drift[0]))
    elif mutation=='foreign-provider':drift[0]['provider_name']='fixture/foreign'
    else:plan['resource_changes']=deepcopy(drift)
    with pytest.raises(PlanError):module.verify_delete_plan(plan,variables,absence_only=True)
