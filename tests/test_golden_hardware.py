"""Hardware real de plantilla: sin defaults de CPU inventados por el plan."""
from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from iacp.terraform.golden_hardware import parse,verify_cpu,verify_live
from iacp.terraform.config import PlanError,Allocation
from iacp.terraform.golden_adapter import install,verify,DIGEST,FILES


def golden(cpu='x86-64-v4,flags=+aes',numa=1):
    return dict(template=1,cpu=cpu,numa=numa,sockets=1,bios='ovmf',machine='q35',scsihw='virtio-scsi-single',hotplug='disk,network,memory',boot='order=scsi0')


@pytest.mark.parametrize('cpu',['x86-64-v4,flags=+aes','host,flags=+aes;-vmx','Haswell-v4'])
def test_cpu_is_resolved_from_each_golden_not_global(cpu):
    h=parse(golden(cpu),29001,'mng01',4)
    assert h.cpu_type==cpu.split(',')[0]
    assert h.numa and h.sockets==1
    assert h.source_cpu==cpu


def test_memory_hotplug_without_numa_rejected_before_plan():
    with pytest.raises(PlanError,match='HOTPLUG_REQUIRES_NUMA'):parse(golden(numa=0),29001,'mng01',1)


@pytest.mark.parametrize('change',['type','flags','numa','cores','sockets'])
def test_provider_defaults_cannot_replace_source_hardware(change):
    h=parse(golden(),29001,'mng01',2).model_dump()
    actual={'cpu':[{'type':h['cpu_type'],'flags':h['flags'],'numa':True,'cores':2,'sockets':1}]}
    variables={'instance':{'cpu_cores':2},'iacp_golden_hardware':h}
    verify_cpu(actual,variables)
    actual['cpu'][0][change]={'type':'qemu64','flags':[],'numa':False,'cores':1,'sockets':2}[change]
    with pytest.raises(PlanError,match='PLAN_MISMATCH'):verify_cpu(actual,variables)


def test_sockets_are_preserved_when_compatible_and_adjusted_only_for_capacity():
    raw={**golden(),'sockets':2}
    assert parse(raw,29001,'mng01',4).sockets==2
    assert parse(raw,29001,'mng01',1).sockets==1


def test_adapter_is_versioned_sealed_and_cannot_enter_legacy_workspaces(tmp_path):
    from iacp.terraform.access_adapter import BASE_MAIN
    for path in FILES:(tmp_path/path).parent.mkdir(parents=True,exist_ok=True)
    (tmp_path/'stacks/kvm-instance/main.tf').write_text(BASE_MAIN)
    install(tmp_path);manifest={'golden_adapter_sha256':DIGEST};variables={'iacp_golden_hardware':{}}
    verify(tmp_path,manifest,variables)
    assert all('x86-64-v4' not in value and 'qemu64' not in value for value in FILES.values())
    with pytest.raises(PlanError):verify(tmp_path,{}, {})
    path=tmp_path/next(iter(FILES));path.write_text('{}')
    with pytest.raises(PlanError):verify(tmp_path,manifest,variables)


def test_native_verification_preserves_firmware_controller_and_hotplug():
    raw=golden();h=parse(raw,29001,'mng01',2).model_dump();clone={**raw,'cores':2,'sockets':1}
    verify_live(clone,h,{'cpu_cores':2})
    for field in ['cpu','numa','bios','machine','scsihw','hotplug']:
        wrong=deepcopy(clone);wrong[field]='qemu64' if field=='cpu' else (0 if field=='numa' else 'incorrecto')
        with pytest.raises(PlanError):verify_live(wrong,h,{'cpu_cores':2})


@pytest.mark.parametrize('password',[False,True])
def test_golden_and_access_adapters_compose_and_seal_the_module_argument(tmp_path,password):
    from iacp.terraform import access_adapter
    from iacp.terraform.golden_adapter import MAIN_ARGUMENT
    for path in FILES:(tmp_path/path).parent.mkdir(parents=True,exist_ok=True)
    for path in access_adapter.FILES:(tmp_path/path).parent.mkdir(parents=True,exist_ok=True)
    main=tmp_path/'stacks/kvm-instance/main.tf';main.write_text(access_adapter.BASE_MAIN)
    variables={'iacp_golden_hardware':{}}
    manifest={'golden_adapter_sha256':DIGEST}
    if password:
        access_adapter.install(tmp_path)
        variables['iacp_root_password']='fixture-no-real'
        manifest['access_adapter_sha256']=access_adapter.DIGEST
    install(tmp_path)
    access_adapter.verify(tmp_path,manifest,variables)
    verify(tmp_path,manifest,variables)
    assert main.read_text().count(MAIN_ARGUMENT)==1
    main.write_text(main.read_text().replace(MAIN_ARGUMENT,''))
    with pytest.raises(PlanError,match='INTEGRITY'):verify(tmp_path,manifest,variables)


def test_delete_records_only_numa_drift_without_changing_sealed_creation_plan():
    from iacp.terraform.deletion_executor import verify_topology
    from tests.test_terraform import allocation_data,plan_json
    allocation=Allocation.model_validate(allocation_data(30000))
    variables=allocation.variables(SimpleNamespace(name='disposable',image='test-image',disk_gb=20,vcpus=2,memory_mb=2048))
    approved=plan_json(variables);approved['resource_changes'][0]['change']['after']['cpu'][0]['numa']=False
    original=deepcopy(approved);current=deepcopy(approved['resource_changes'][0]['change']['after'])
    current['cpu'][0]['numa']=True;persisted=deepcopy(current)
    assert verify_topology(approved,current,persisted=persisted)==['cpu.numa']
    assert approved==original and current==persisted
    current['cpu'][0]['cores']=8
    with pytest.raises(PlanError):verify_topology(approved,current,persisted=persisted)
