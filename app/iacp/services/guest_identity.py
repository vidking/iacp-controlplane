"""A fixed, profile-approved initializer. No caller-controlled guest commands/files.

POST is fenced before dispatch. A consumed intent is only observed, even when
the response (and PID) was lost. Guest Agent is reached without guest networking.
"""
import hashlib
import json
import re
import ssl
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, build_opener
from uuid import UUID, uuid4

from sqlalchemy import select

from ..lifecycle.certification_binding import binding_digest
from ..lifecycle.proxmox import PowerError, ProxmoxPower, granted
from ..lifecycle.service import managed_target
from ..models import GuestInitialization, Instance, Job, Operation, Tenant, TerraformExecution, utcnow
from ..terraform.apply import fingerprint
from ..terraform.locks import ExecutionBusy, admission_lock, instance_lock
from ..terraform.observation import NoRedirect
from .instances import Conflict, event
from .jobs import QueueUnavailable

MODE = 'ubuntu-2404-lab-identity-v1'
MARKER = '/var/lib/iacp/guest-identity.json'
NETWORK_FILE = '/etc/netplan/99-iacp-identity.yaml'


class GuestProgramFailed(Conflict):
    """The consumed process has definitively exited unsuccessfully."""

# The only executable program this adapter can submit. No shell, remote download,
# package install, credential change, arbitrary path, power action or state edit.
PROGRAM = r'''
import hashlib,json,os,re,subprocess,sys,tempfile,uuid
from pathlib import Path
import yaml
identifier,mac,program_sha=sys.argv[1:]
assert str(uuid.UUID(identifier))==identifier and re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',mac)
assert re.fullmatch(r'[0-9a-f]{64}',program_sha)
release=Path('/etc/os-release').read_text()
assert 'ID=ubuntu\n' in release and 'VERSION_ID="24.04"' in release
interfaces=[p for p in Path('/sys/class/net').glob('*/address') if p.read_text().strip().lower()==mac]
assert len(interfaces)==1
ifindex=int((interfaces[0].parent/'ifindex').read_text())
subprocess.run(['/usr/bin/cloud-init','status','--wait'],capture_output=True,timeout=90)
assert Path('/var/lib/cloud/instance/boot-finished').is_file()
config=yaml.safe_load(Path('/etc/netplan/50-cloud-init.yaml').read_text())['network']
assert config['version']==2 and set(config)<= {'version','renderer','ethernets'}
assert config.get('renderer','networkd')=='networkd' and len(config['ethernets'])==1
name,ethernet=next(iter(config['ethernets'].items()))
assert re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',name)
assert ethernet['match']['macaddress'].lower()==mac and ethernet.get('dhcp4') is True
assert not any(ethernet.get(k) for k in ['addresses','gateway4','gateway6','routes'])
marker=Path('/var/lib/iacp/guest-identity.json')
assert not marker.exists(), 'An initializer never overwrites a previous identity'
network=Path('/etc/netplan/99-iacp-identity.yaml')
assert not network.exists()
def write(path,value):
    assert not path.is_symlink()
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.iacp-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
content=json.dumps({'network':{'version':2,'ethernets':{name:{'dhcp-identifier':'mac'}}}},sort_keys=True)+'\n'
write(network,content)
subprocess.run(['/usr/sbin/netplan','generate'],check=True,capture_output=True,timeout=30)
machine=uuid.uuid4().hex+'\n'
write(Path('/etc/machine-id'),machine)
os.chmod('/etc/machine-id',0o444)
dbus=Path('/var/lib/dbus/machine-id')
if dbus.is_symlink():
    assert dbus.resolve()==Path('/etc/machine-id')
elif dbus.exists():
    write(dbus,machine);os.chmod(dbus,0o444)
subprocess.run(['/usr/sbin/netplan','apply'],check=True,capture_output=True,timeout=60)
result={'instance_id':identifier,'mac_address':mac,'mode':'ubuntu-2404-lab-identity-v1',
        'program_sha256':program_sha,'machine_id_sha256':hashlib.sha256(machine.encode()).hexdigest(),
        'network_sha256':hashlib.sha256(content.encode()).hexdigest(),'ifindex':ifindex}
write(marker,json.dumps(result,sort_keys=True)+'\n')
'''
PROGRAM_SHA256 = hashlib.sha256(PROGRAM.encode()).hexdigest()


def active(session, instance_id):
    return session.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance_id,
        GuestInitialization.status.in_(['queued','running','uncertain'])))


def configured_mode(session, instance, *, current_profiles=True):
    from ..services.lab import member
    row=member(session,instance,'start',current_profiles=current_profiles)
    mode=row.profile_snapshots[instance.network_profile].get('guest_initialization')
    if mode!=MODE or row.profile_snapshots[instance.network_profile].get('guest_initialization_program_sha256')!=PROGRAM_SHA256:
        raise Conflict('GUEST_INITIALIZATION_NOT_AUTHORIZED')
    return mode


def validate_binding(session, row, operation):
    instance=session.get(Instance,row.instance_id,populate_existing=True)
    tenant=session.get(Tenant,operation.tenant_id,populate_existing=True)
    if (not instance or not tenant or not tenant.active or instance.tenant_id!=operation.tenant_id
            or operation.instance_id!=instance.id or operation.kind!='instance.network.initialize'):
        raise Conflict('GUEST_INITIALIZATION_BINDING_CHANGED')
    target,approval=managed_target(session,instance)
    execution=session.get(TerraformExecution,instance.id)
    mac=(instance.result or {}).get('network',{}).get('mac_address','').lower()
    if (target.vmid<30000 or target.vmid in {104,106} or not instance.self_service_policy_id
            or instance.network_mode!='dhcp' or row.mode!=MODE or mac!=row.mac_address
            or row.provisioning_binding!=binding_digest(instance,execution,approval)):
        raise Conflict('GUEST_INITIALIZATION_BINDING_CHANGED')
    return instance,target


def request_initialization(session, instance, principal, key, publish, *, current_profiles=True):
    from .lab import authorize_operation
    from ..lifecycle.ownership import mutation_revision
    from ..lifecycle.service import response
    digest=fingerprint({'instance_id':instance.id,'action':'network.initialize'})
    if principal.tenant_id!=instance.tenant_id:raise Conflict('GUEST_INITIALIZATION_BINDING_CHANGED')
    try:
        with admission_lock(session),instance_lock(session.get_bind(),instance.id):
            instance=session.get(Instance,instance.id,populate_existing=True)
            previous=session.scalar(select(Operation).where(Operation.tenant_id==principal.tenant_id,Operation.idempotency_key==key))
            if previous:
                if (previous.instance_id!=instance.id or previous.requested_by!=principal.subject
                        or previous.request_hash!=digest or previous.kind!='instance.network.initialize'):
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                operation=previous
                row=session.get(GuestInitialization,previous.id)
                if not row:raise Conflict('GUEST_INITIALIZATION_BINDING_CHANGED')
                validate_binding(session,row,operation)
                if row.status=='failed' and not row.started_at:
                    configured_mode(session,instance,current_profiles=current_profiles);mutation_revision()
                    authorize_operation(session,instance,'start')
                    row.status=operation.status='queued';operation.error_code=None
                    session.scalar(select(Job).where(Job.operation_id==operation.id)).status='queued'
                    event(session,operation,'guest_initialization_retry_before_dispatch',principal.subject)
            else:
                if session.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance.id)):
                    raise Conflict('GUEST_INITIALIZATION_ALREADY_EXISTS; use its operation or reconcile')
                mode=configured_mode(session,instance,current_profiles=current_profiles);mutation_revision()
                authorize_operation(session,instance,'start')
                from ..terraform.deletion import active_deletion
                from ..models import PowerExecution
                if active_deletion(session,instance.id) or session.scalar(select(PowerExecution).where(
                        PowerExecution.instance_id==instance.id,PowerExecution.status.in_(['queued','running','uncertain']))):
                    raise Conflict('INSTANCE_BUSY')
                target,approval=managed_target(session,instance)
                execution=session.get(TerraformExecution,instance.id)
                mac=(instance.result or {}).get('network',{}).get('mac_address','').lower()
                if not re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',mac):raise Conflict('GUEST_INITIALIZATION_MAC_REQUIRED')
                operation=Operation(id=str(uuid4()),tenant_id=instance.tenant_id,instance_id=instance.id,
                    kind='instance.network.initialize',status='queued',idempotency_key=key,request_hash=digest,requested_by=principal.subject)
                session.add(operation);session.flush()
                row=GuestInitialization(operation_id=operation.id,instance_id=instance.id,mode=mode,mac_address=mac,
                    program_sha256=PROGRAM_SHA256,provisioning_binding=binding_digest(instance,execution,approval),status='queued')
                session.add(row)
                session.add(Job(id=str(uuid4()),tenant_id=instance.tenant_id,operation_id=operation.id,operation=operation.kind,status='queued'))
                instance.ip_addresses=[]
                instance.network_observation={'status':'pending','ip_addresses':[],'reason':'GUEST_INITIALIZATION_REQUIRED'}
                event(session,operation,'guest_initialization_accepted',principal.subject)
            session.commit()
            result=response(session,operation)
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None
    except PowerError as failure:
        raise Conflict(failure.code) from None
    if row.status in {'queued','running','uncertain'}:
        try:publish(operation.id)
        except Exception:raise QueueUnavailable from None
    return result


class GuestClient:
    @classmethod
    def configured(cls):
        from ..terraform.lifecycle import build_executor
        with tempfile.TemporaryDirectory() as temporary:
            env=build_executor(observation=True).environment(Path(temporary))
        return cls(ProxmoxPower(env['PROXMOX_VE_ENDPOINT'].rstrip('/'),env['PROXMOX_VE_API_TOKEN'],
            build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=env.get('SSL_CERT_FILE'))),NoRedirect())))

    def __init__(self,client):self.client=client

    def check(self,target,mac):
        if self.client.observe(target,preflight=True)['power_state']!='running':raise Conflict('GUEST_INITIALIZATION_VM_NOT_RUNNING')
        config=self.client._request('GET',target.path+'/config?current=1')
        nic=dict(p.split('=',1) for p in config.get('net0','').split(',') if '=' in p)
        if (nic.get('virtio','').lower()!=mac or nic.get('bridge')!='vmbr0'
                or 'tag' in nic or 'trunks' in nic or config.get('ipconfig0')!='ip=dhcp'):
            raise Conflict('GUEST_INITIALIZATION_BINDING_CHANGED')
        permissions=self.client._request('GET','/access/permissions?'+urlencode({'path':'/vms/'+str(target.vmid)}))
        if not granted(permissions.get('/vms/'+str(target.vmid),{}),'VM.GuestAgent.Unrestricted'):
            raise Conflict('GUEST_INITIALIZATION_PERMISSION_REQUIRED')

    def ready(self,target):
        self.client._request('GET',target.path+'/agent/info')

    def submit(self,target,instance_id,mac):
        assert str(UUID(instance_id))==instance_id and re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',mac)
        # PVE array form encoding: repeated command parameters, never shell quoting.
        result=self.client._request('POST',target.path+'/agent/exec',[
            ('command','/usr/bin/python3'),('command','-c'),('command',PROGRAM),
            ('command',instance_id),('command',mac),('command',PROGRAM_SHA256)])
        pid=result.get('pid')
        if type(pid) is not int or pid<=0:raise Conflict('GUEST_INITIALIZATION_PID_INVALID')
        return pid

    def observe(self,target,row):
        process=None
        if row.pid:
            try:process=self.client._request('GET',target.path+'/agent/exec-status?'+urlencode({'pid':row.pid}))
            except PowerError:pass  # QGA may have reaped the result; verify durable guest evidence.
            if process and not process.get('exited'):return None
            if process and process.get('exitcode')!=0:raise GuestProgramFailed('GUEST_INITIALIZATION_PROGRAM_FAILED')
        def read(path):
            value=self.client._request('GET',target.path+'/agent/file-read?'+urlencode({'file':path,'count':8192}))
            if value.get('truncated'):raise Conflict('GUEST_INITIALIZATION_EVIDENCE_INVALID')
            return value['content']
        evidence=json.loads(read(MARKER))
        if type(evidence.get('ifindex')) is not int or not 1<=evidence['ifindex']<=65535:
            raise Conflict('GUEST_INITIALIZATION_EVIDENCE_INVALID')
        if (evidence.get('instance_id')!=row.instance_id or evidence.get('mac_address')!=row.mac_address
                or evidence.get('mode')!=row.mode or evidence.get('program_sha256')!=row.program_sha256
                or evidence.get('machine_id_sha256')!=hashlib.sha256(read('/etc/machine-id').encode()).hexdigest()
                or evidence.get('network_sha256')!=hashlib.sha256(read(NETWORK_FILE).encode()).hexdigest()):
            raise Conflict('GUEST_INITIALIZATION_EVIDENCE_INVALID')
        from .network import agent_addresses
        network=agent_addresses(self.client._request('GET',target.path+'/agent/network-get-interfaces'),row.mac_address)
        if not network['ip_addresses']:return None
        try:lease_content=read('/run/systemd/netif/leases/'+str(evidence['ifindex']))
        except PowerError:return None  # networkd replaces the lease while DHCP converges.
        lease=dict(line.split('=',1) for line in lease_content.splitlines() if '=' in line)
        clientid=lease.get('CLIENTID','').lower().replace(':','')
        if clientid!='01'+row.mac_address.replace(':','') or lease.get('ADDRESS') not in network['ip_addresses']:return None
        return {**evidence,'network':network,'source':'qemu-guest-agent','guest_ready':None}


def execute_initialization(identifier,sessions,client_factory=GuestClient.configured,timeout=210):
    with sessions() as s:
        row=s.get(GuestInitialization,identifier)
        if row is None or row.status in {'completed','failed'}:return
        instance_id=row.instance_id
    try:
        with instance_lock(sessions.kw['bind'],instance_id) as heartbeat:
            with sessions() as s:
                row=s.get(GuestInitialization,identifier,with_for_update=True)
                if row.status in {'completed','failed'}:return
                operation=s.get(Operation,identifier)
                job=s.scalar(select(Job).where(Job.operation_id==identifier))
                try:
                    if not job or job.operation!=operation.kind or job.tenant_id!=operation.tenant_id:
                        raise Conflict('GUEST_INITIALIZATION_BINDING_CHANGED')
                    instance,target=validate_binding(s,row,operation)
                    client=client_factory();client.check(target,row.mac_address)
                    deadline=time.monotonic()+timeout
                    if not row.started_at:
                        configured_mode(s,instance)
                        from ..lifecycle.ownership import mutation_revision
                        mutation_revision()
                        if row.program_sha256!=PROGRAM_SHA256:raise Conflict('GUEST_INITIALIZATION_PROGRAM_CHANGED')
                        while True:
                            try:client.ready(target);break
                            except PowerError:
                                if time.monotonic()>=deadline:raise Conflict('GUEST_AGENT_UNAVAILABLE') from None
                                heartbeat();time.sleep(2)
                        # Revalidate immediately before consuming the only POST fence.
                        s.refresh(instance);configured_mode(s,instance);mutation_revision();client.check(target,row.mac_address)
                        row.started_at=utcnow();row.status=operation.status=job.status='running'
                        event(s,operation,'guest_initialization_dispatch_consumed','worker:iac');s.commit()
                        row.pid=client.submit(target,instance.id,row.mac_address);s.commit()
                    while True:
                        validate_binding(s,row,operation)
                        result=client.observe(target,row)
                        if result:
                            others=s.scalars(select(Instance).where(Instance.id!=instance.id,Instance.status!='deleted',
                                Instance.network_mode=='dhcp')).all()
                            if any(set(result['network']['ip_addresses']) & set(other.ip_addresses or []) for other in others):
                                raise Conflict('DHCP_ADDRESS_CONFLICT')
                            row.result=result;row.status=operation.status=job.status='completed';row.finished_at=utcnow()
                            operation.error_code=None
                            instance.ip_addresses=result['network']['ip_addresses']
                            instance.network_observation={**result['network'],'observed_at':utcnow().isoformat(),
                                'identity_initialization':'completed','initialization_operation_id':identifier}
                            event(s,operation,'guest_initialization_verified','worker:iac');s.commit();return
                        if time.monotonic()>=deadline:raise Conflict('GUEST_INITIALIZATION_OBSERVATION_TIMEOUT')
                        heartbeat();time.sleep(2)
                except Exception as failure:
                    # Never reset a consumed fence or publish a second exec.
                    row.status=operation.status=('uncertain' if row.started_at and not isinstance(failure,GuestProgramFailed) else 'failed')
                    if isinstance(failure,GuestProgramFailed):row.finished_at=utcnow()
                    if job:job.status='failed'
                    operation.error_code=(str(failure)[:64] if isinstance(failure,Conflict) else failure.code
                        if isinstance(failure,PowerError) else 'GUEST_INITIALIZATION_OBSERVATION_FAILED')
                    event(s,operation,'guest_initialization_'+row.status,'worker:iac');s.commit()
    except ExecutionBusy:return
