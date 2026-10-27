"""Hardware real de plantilla: sin defaults de CPU inventados por el plan."""
from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from iacp.terraform.golden_hardware import parse,verify_base_plan,verify_cpu,verify_live,verify_disk_plan,_storage_observation
from iacp.terraform.config import PlanError,Allocation
from iacp.terraform.golden_adapter import install,verify,DIGEST,FILES


def golden(cpu='x86-64-v4,flags=+aes',numa=1,hotplug='disk,network,memory',disk='DS_CYBERFUEL:base-29001-disk-1,discard=on,iothread=1,size=20G'):
    return dict(template=1,cpu=cpu,numa=numa,sockets=1,bios='ovmf',machine='q35',scsihw='virtio-scsi-single',hotplug=hotplug,boot='order=scsi0',scsi0=disk)


@pytest.mark.parametrize('cpu',['x86-64-v4,flags=+aes','host,flags=+aes;-vmx','Haswell-v4'])
def test_cpu_is_resolved_from_each_golden_not_global(cpu):
    h=parse(golden(cpu),29001,'mng01',4)
    assert h.cpu_type==cpu.split(',')[0]
    assert h.numa and h.sockets==1
    assert h.source_cpu==cpu


@pytest.mark.parametrize('template,cpu,scsihw', [
    (29000, 'x86-64-v4,flags=+aes', 'virtio-scsi-single'),
    (29001, 'x86-64-v3,flags=+aes', 'virtio-scsi-pci'),
    (29002, 'host,flags=+aes;-pcid', 'virtio-scsi-single'),
    (29003, 'Haswell-v4', 'lsi'),
])
def test_each_catalog_golden_carries_its_own_hardware_contract(template, cpu, scsihw):
    raw={**golden(cpu=cpu), 'scsihw': scsihw}
    hardware=parse(raw, template, 'mng01', 1).model_dump()
    variables={'instance': {'cpu_cores': 1}, 'iacp_golden_hardware': hardware}
    verify_cpu({'cpu': [{'type': hardware['cpu_type'], 'flags': hardware['flags'],
                         'numa': hardware['numa'], 'cores': 1, 'sockets': 1,
                         'hotplugged': 1}]}, variables)
    verify_base_plan(hardware['terraform_base'], variables)


def test_memory_hotplug_without_numa_rejected_before_plan():
    with pytest.raises(PlanError,match='HOTPLUG_REQUIRES_NUMA'):parse(golden(numa=0),29001,'mng01',1)


@pytest.mark.parametrize('change',['type','flags','numa','cores','sockets'])
def test_provider_defaults_cannot_replace_source_hardware(change):
    h=parse(golden(),29001,'mng01',2).model_dump()
    actual={'cpu':[{'type':h['cpu_type'],'flags':h['flags'],'numa':True,'cores':2,'sockets':1,
                    'hotplugged':2}]}
    variables={'instance':{'cpu_cores':2},'iacp_golden_hardware':h}
    verify_cpu(actual,variables)
    actual['cpu'][0][change]={'type':'qemu64','flags':[],'numa':False,'cores':1,'sockets':2}[change]
    with pytest.raises(PlanError,match='PLAN_MISMATCH'):verify_cpu(actual,variables)


def test_template_vcpu_and_resources_are_not_inherited_by_kvm1_plan():
    """Regresión del clon 30057: cores/vcpus/RAM/disco del template no son el plan."""
    hardware=parse({**golden(), 'cores': 10, 'vcpus': 2, 'memory': 4096}, 29000, 'mng01', 1).model_dump()
    variables={'instance': {'cpu_cores': 1}, 'iacp_golden_hardware': hardware}

    # El plan materializado debe expresar explícitamente 1 core y 1 vCPU.
    verify_cpu({'cpu': [{'type': hardware['cpu_type'], 'flags': hardware['flags'],
                         'numa': hardware['numa'], 'cores': 1, 'sockets': 1,
                         'hotplugged': 1}]}, variables)

    # Cualquier herencia de la topología del template se bloquea antes de apply.
    inherited={'cpu': [{'type': hardware['cpu_type'], 'flags': hardware['flags'],
                        'numa': hardware['numa'], 'cores': 10, 'sockets': 1,
                        'hotplugged': 2}]}
    with pytest.raises(PlanError, match='PLAN_MISMATCH'):
        verify_cpu(inherited, variables)


def test_sockets_are_preserved_when_compatible_and_adjusted_only_for_capacity():
    raw={**golden(),'sockets':2}
    assert parse(raw,29001,'mng01',4).sockets==2
    assert parse(raw,29001,'mng01',1).sockets==1


def test_adapter_is_versioned_sealed_and_cannot_enter_legacy_workspaces(tmp_path):
    from iacp.terraform.access_adapter import BASE_MAIN
    for path in FILES:(tmp_path/path).parent.mkdir(parents=True,exist_ok=True)
    (tmp_path/'stacks/kvm-instance').mkdir(parents=True,exist_ok=True)
    (tmp_path/'stacks/kvm-instance/main.tf').write_text(BASE_MAIN)
    install(tmp_path);manifest={'golden_adapter_sha256':DIGEST};variables={'iacp_golden_hardware':{}}
    verify(tmp_path,manifest,variables)
    assert all('x86-64-v4' not in value and 'qemu64' not in value for value in FILES.values())
    override=json.loads(FILES['modules/compute/kvm/iacp_golden_override.tf.json'])
    resource=override['resource']['proxmox_virtual_environment_vm']['this']
    assert resource['cpu'][0]['type']=='${var.golden_hardware.cpu_type}'
    assert resource['cpu'][0]['hotplugged']=='${var.cpu_cores}'
    assert resource['bios']=='${try(var.golden_hardware.terraform_base.bios, null)}'
    assert resource['scsi_hardware']=='${try(var.golden_hardware.terraform_base.scsi_hardware, null)}'
    with pytest.raises(PlanError):verify(tmp_path,{}, {})
    path=tmp_path/next(iter(FILES));path.write_text('{}')
    with pytest.raises(PlanError):verify(tmp_path,manifest,variables)


def test_native_verification_preserves_firmware_controller_and_hotplug():
    raw=golden();h=parse(raw,29001,'mng01',2).model_dump();clone={**raw,'cores':2,'sockets':1,'vcpus':2}
    expected={'cpu_cores':2,'boot_disk':{'datastore_id':'DS_CYBERFUEL','size_gb':20}}
    verify_live(clone,h,expected)
    for field in ['cpu','numa','bios','machine','scsihw','hotplug']:
        wrong=deepcopy(clone);wrong[field]='qemu64' if field=='cpu' else (0 if field=='numa' else 'incorrecto')
        with pytest.raises(PlanError):verify_live(wrong,h,expected)


def test_live_verification_accepts_provider_cputype_prefix_only_for_same_cpu():
    h=parse(golden(hotplug='disk,network,memory'),29001,'mng01',2).model_dump()
    clone=golden(hotplug='disk,network,memory')
    clone.update(cpu='cputype=x86-64-v4,flags=+aes',cores=2,sockets=1,vcpus=2)
    verify_live(clone,h,{'cpu_cores':2,'boot_disk':{'datastore_id':'DS_CYBERFUEL','size_gb':20}})
    clone['cpu']='cputype=qemu64,flags=+aes'
    with pytest.raises(PlanError,match='POSTCONDITION_MISMATCH'):
        verify_live(clone,h,{'cpu_cores':2,'boot_disk':{'datastore_id':'DS_CYBERFUEL','size_gb':20}})


def test_golden_disk_snapshot_preserves_only_observed_options():
    h=parse(golden(),29001,'mng01',2).model_dump()
    disk=h['disk']
    assert disk['interface']=='scsi0'
    assert disk['minimum_size_gb']==20
    assert disk['discard']=='on' and disk['iothread'] is True
    assert disk['aio'] is None and disk['backup'] is None and disk['replicate'] is None
    assert h['terraform_base'] == {
        'bios': 'ovmf', 'machine': 'q35', 'scsi_hardware': 'virtio-scsi-single',
        'hotplug': 'disk,network,memory', 'boot_order': ['scsi0'],
    }


def test_storage_observation_falls_back_to_authorized_node_scope():
    from iacp.lifecycle.proxmox import PowerError

    class Client:
        def __init__(self):
            self.paths = []

        def _request(self, method, path):
            self.paths.append(path)
            if path == '/storage/DS_CYBERFUEL':
                raise PowerError('POWER_PERMISSION_DENIED')
            assert path == '/nodes/mng01/storage/DS_CYBERFUEL/status'
            return {'type': 'rbd', 'active': 1, 'enabled': 1}

    client = Client()
    assert _storage_observation(client, 'mng01', 'DS_CYBERFUEL') == ('rbd', False)
    assert client.paths == ['/storage/DS_CYBERFUEL', '/nodes/mng01/storage/DS_CYBERFUEL/status']


def test_plan_barrier_rejects_firmware_controller_and_boot_defaults():
    hardware=parse(golden(),29001,'mng01',1).model_dump()
    variables={'iacp_golden_hardware': hardware}
    plan={
        'bios': 'ovmf', 'machine': 'q35', 'scsi_hardware': 'virtio-scsi-single',
        'hotplug': 'disk,network,memory', 'boot_order': ['scsi0'],
    }
    verify_base_plan(plan, variables)
    for key, value in [('bios','seabios'), ('machine',None),
                       ('scsi_hardware','virtio-scsi-pci'), ('boot_order',None)]:
        wrong=dict(plan); wrong[key]=value
        with pytest.raises(PlanError, match='PLAN_MISMATCH'):
            verify_base_plan(wrong, variables)


def test_disk_plan_allows_only_verified_rbd_defaults_not_present_in_golden():
    hardware=parse(golden(),29001,'mng01',1).model_dump()
    hardware['disk']['storage_type'] = 'rbd'
    hardware['disk']['storage_krbd'] = False
    variables={'instance':{'boot_disk':{'interface':'scsi0','size_gb':50,'datastore_id':'DS_CYBERFUEL'}},
               'iacp_golden_hardware':hardware}
    plan={'interface':'scsi0','size':50,'datastore_id':'DS_CYBERFUEL',
          'file_format':None,'aio':None,'cache':None,'discard':'on','backup':None,
          'replicate':None,'iothread':True,'ssd':None,'serial':None,'queues':None}
    verify_disk_plan(plan,variables)
    for key, value in {
        'aio': 'io_uring', 'backup': True, 'cache': 'none',
        'replicate': True, 'ssd': False, 'queues': 0,
    }.items():
        verified=dict(plan); verified[key]=value
        verify_disk_plan(verified,variables)
        hardware['disk']['storage_type'] = 'unknown'
        with pytest.raises(PlanError,match='DISK_PLAN_MISMATCH'):
            verify_disk_plan(verified,variables)
        hardware['disk']['storage_type'] = 'rbd'
    wrong=dict(plan); wrong['iothread']=False
    with pytest.raises(PlanError,match='DISK_PLAN_MISMATCH'):
        verify_disk_plan(wrong,variables)


def test_verified_defaults_never_relax_discard_or_iothread():
    hardware=parse(golden(),29001,'mng01',1).model_dump()
    hardware['disk']['storage_type'] = 'rbd'
    hardware['disk']['storage_krbd'] = False
    variables={'instance':{'boot_disk':{'interface':'scsi0','size_gb':50,'datastore_id':'DS_CYBERFUEL'}},
               'iacp_golden_hardware':hardware}
    for key, value in {'discard': None, 'iothread': False}.items():
        wrong={'interface':'scsi0','size':50,'datastore_id':'DS_CYBERFUEL',
               'file_format':None,'aio':'io_uring','cache':'none','discard':'on',
               'backup':True,'replicate':True,'iothread':True,'ssd':False,
               'serial':None,'queues':0}
        wrong[key] = value
        with pytest.raises(PlanError,match='DISK_PLAN_MISMATCH'):
            verify_disk_plan(wrong,variables)


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
