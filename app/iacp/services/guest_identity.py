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

from ..lifecycle.proxmox import PowerError, ProxmoxPower, granted
from ..lifecycle.service import managed_target
from ..models import GuestInitialization, Instance, Job, Operation, OperationEvent, Tenant, TerraformExecution, utcnow
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


GUEST_INITIALIZER_COMMANDS = frozenset({
    "guest-exec",
    "guest-exec-status",
    "guest-file-read",
})

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

# A separately reviewed variant for root-password lab profiles. No credentials
# enter guest-exec arguments or files; Cloud-Init alone sets the initial password.
ROOT_SSH_CONFIG = 'PermitRootLogin yes\nPasswordAuthentication yes\nKbdInteractiveAuthentication no\n'
ROOT_POLICY_PROGRAM = r'''
ssh_config=Path('/etc/ssh/sshd_config.d/00-iacp-root-lab.conf')
# ssh.socket may be listening before ssh.service creates its RuntimeDirectory.
Path('/run/sshd').mkdir(mode=0o755,exist_ok=True)
write(ssh_config,'PermitRootLogin yes\nPasswordAuthentication yes\nKbdInteractiveAuthentication no\n')
subprocess.run(['/usr/sbin/sshd','-t'],check=True,capture_output=True,timeout=10)
effective=subprocess.run(['/usr/sbin/sshd','-T','-C','user=root,host=localhost,addr=127.0.0.1'],check=True,capture_output=True,text=True,timeout=10).stdout
assert 'permitrootlogin yes\n' in effective and 'passwordauthentication yes\n' in effective
subprocess.run(['/usr/bin/systemctl','reload-or-restart','ssh'],check=True,capture_output=True,timeout=15)
'''
ROOT_PROGRAM = PROGRAM.replace("write(marker,json.dumps(result,sort_keys=True)+'\\n')",
    ROOT_POLICY_PROGRAM + "\nwrite(marker,json.dumps(result,sort_keys=True)+'\\n')")
ROOT_PROGRAM_SHA256 = hashlib.sha256(ROOT_PROGRAM.encode()).hexdigest()

# La inicialización técnica de una instancia nueva también configura el acceso
# solicitado por el consumidor. La contraseña entra únicamente por stdin de
# QGA; nunca forma parte de los argumentos del proceso ni del marcador.
TECHNICAL_ACCESS_PROGRAM = r'''
payload=json.loads(sys.stdin.read())
assert isinstance(payload,dict) and set(payload)=={'password','ssh_public_keys'}
password=payload['password']
keys=payload['ssh_public_keys']
assert isinstance(password,str) and 16<=len(password)<=128 and all(32<=ord(c)<=126 for c in password)
assert isinstance(keys,list) and len(keys)<=16
assert all(isinstance(key,str) and key.startswith(('ssh-ed25519 ','ssh-rsa ','ecdsa-sha2-'))
           and '\n' not in key and '\r' not in key for key in keys)
ssh_directory=Path('/root/.ssh')
ssh_directory.mkdir(mode=0o700,parents=True,exist_ok=True)
os.chmod(ssh_directory,0o700);os.chown(ssh_directory,0,0)
if keys:
    write(ssh_directory/'authorized_keys','\n'.join(keys)+'\n')
    os.chmod(ssh_directory/'authorized_keys',0o600)
    os.chown(ssh_directory/'authorized_keys',0,0)
ssh_config=Path('/etc/ssh/sshd_config.d/00-iacp-technical-root.conf')
write(ssh_config,'PermitRootLogin yes\nPasswordAuthentication yes\nKbdInteractiveAuthentication no\n')
subprocess.run(['/usr/sbin/sshd','-t'],check=True,capture_output=True,timeout=10)
effective=subprocess.run(['/usr/sbin/sshd','-T','-C','user=root,host=localhost,addr=127.0.0.1'],
    check=True,capture_output=True,text=True,timeout=10).stdout
assert 'permitrootlogin yes\n' in effective and 'passwordauthentication yes\n' in effective
subprocess.run(['/usr/bin/chpasswd'],input=('root:'+password+'\n').encode(),
    check=True,capture_output=True,timeout=15)
service='ssh' if 'ID=ubuntu\n' in release else 'sshd'
subprocess.run(['/usr/bin/systemctl','reload-or-restart',service],check=True,capture_output=True,timeout=20)
if 'ID=almalinux\n' in release:
    for path in ['/root/.ssh','/etc/ssh/sshd_config.d/00-iacp-technical-root.conf']:
        if Path(path).exists():
            subprocess.run(['/usr/sbin/restorecon','-RF',path],check=True,capture_output=True,timeout=15)
authorized_keys_sha256=hashlib.sha256(('\n'.join(keys)+'\n').encode()).hexdigest()
result['technical_access']={'password_configured':True,
    'ssh_public_keys_sha256':authorized_keys_sha256}
password=None
'''


def _technical_program(base):
    marker_write = "write(marker,json.dumps(result,sort_keys=True)+'\\n')"
    if base.count(marker_write) != 1:
        raise Conflict('GUEST_INITIALIZATION_PROGRAM_INVALID')
    return base.replace(marker_write, TECHNICAL_ACCESS_PROGRAM + "\n" + marker_write, 1)


def technical_program_for_mode(mode):
    """Devuelve el único programa técnico de acceso para una imagen autorizada."""
    base, _ = program_for_mode(mode)
    program = _technical_program(base)
    return program, hashlib.sha256(program.encode()).hexdigest()


def program_for_mode(mode, *, root=False):
    if mode == MODE:
        return (ROOT_PROGRAM, ROOT_PROGRAM_SHA256) if root else (PROGRAM, PROGRAM_SHA256)
    if root:
        raise Conflict('GUEST_INITIALIZATION_NOT_AUTHORIZED')
    from .guest_image_programs import image_program
    try:
        return image_program(mode, PROGRAM)
    except ValueError:
        raise Conflict('GUEST_INITIALIZATION_NOT_AUTHORIZED') from None


def active(session, instance_id):
    return session.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance_id,
        GuestInitialization.status.in_(['queued','running','uncertain'])))


def supersede_queued_for_delete(session, instance, delete_operation_id, actor):
    """Honor terminal initialization; retire only a queued, unconsumed one.

    The authorized DELETE transaction owns the instance lock. Historical
    failed-without-dispatch and verified completed initializations are already
    terminal: leave their operation, job and evidence unmodified. Any ambiguous
    execution remains fenced and requires reconciliation.
    """
    row = session.scalar(select(GuestInitialization).where(
        GuestInitialization.instance_id == instance.id).with_for_update())
    if row is None:
        return False
    operation = session.get(Operation, row.operation_id, with_for_update=True)
    job = session.scalar(select(Job).where(Job.operation_id == row.operation_id).with_for_update())
    consumed = session.scalar(select(OperationEvent.id).where(
        OperationEvent.operation_id == row.operation_id,
        OperationEvent.code == 'guest_initialization_dispatch_consumed').limit(1))
    delete_operation = session.get(Operation, delete_operation_id, with_for_update=True)
    if (delete_operation is None or delete_operation.instance_id != instance.id
            or delete_operation.tenant_id != instance.tenant_id
            or delete_operation.kind != 'instance.delete'):
        raise Conflict('DELETE_BINDING_CHANGED')
    if (operation is None or operation.instance_id != instance.id
            or operation.tenant_id != instance.tenant_id
            or operation.kind != 'instance.network.initialize'
            or (job is not None and (job.operation_id != operation.id
                or job.tenant_id != instance.tenant_id or job.operation != operation.kind))):
        raise Conflict('GUEST_INITIALIZATION_RECONCILIATION_REQUIRED')
    if row.status == 'failed':
        # The earlier supersession or admin recovery is already terminal.
        # finished_at may be populated; it does not constitute a guest-exec.
        if (operation.status != 'failed' or (job is not None and job.status != 'failed')
                or row.started_at is not None or row.pid is not None
                or row.result is not None or consumed is not None):
            raise Conflict('GUEST_INITIALIZATION_RECONCILIATION_REQUIRED')
        return False
    if row.status == 'completed':
        # Successfully completed work is no longer an active fence. Do not
        # reset or supersede its immutable execution evidence.
        if (operation.status != 'completed' or (job is not None and job.status != 'completed')
                or row.started_at is None or row.finished_at is None or row.result is None):
            raise Conflict('GUEST_INITIALIZATION_RECONCILIATION_REQUIRED')
        return False
    if (row.status != 'queued' or operation.status != 'queued'
            or row.started_at is not None or row.pid is not None or row.finished_at is not None
            or row.result is not None or consumed is not None
            or (job is not None and job.status != 'queued')):
        raise Conflict('GUEST_INITIALIZATION_RECONCILIATION_REQUIRED')
    row.status = operation.status = 'failed'
    row.finished_at = utcnow()
    operation.error_code = 'GUEST_INITIALIZATION_SUPERSEDED_BY_DELETE'
    if job is not None:
        job.status = 'failed'
    code = 'guest_initialization_superseded_by_delete'
    event(session, operation, code, 'delete:' + delete_operation.id)
    event(session, delete_operation, code, actor)
    return True


def configured_mode(session, instance, *, current_profiles=True):
    from .authorization import member
    from .contracts import instance_uses_technical_access
    row=member(session,instance,'start',current_profiles=current_profiles)
    mode=row.profile_snapshots[instance.network_profile].get('guest_initialization')
    technical = instance_uses_technical_access(instance)
    expected = (technical_program_for_mode(mode)[1] if technical else
                program_for_mode(mode, root=bool(row.profile_snapshots[instance.network_profile].get('root_password_bootstrap')))[1])
    if row.profile_snapshots[instance.network_profile].get('guest_initialization_program_sha256')!=expected:
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
    from .technical_ownership import record as technical_record
    ownership=technical_record(session, instance, adopt=True)
    mac=(instance.result or {}).get('network',{}).get('mac_address','').lower()
    from .technical_ownership import is_managed_vmid
    if (not is_managed_vmid(target.vmid)
            or instance.network_mode!='dhcp' or row.mode not in {MODE, 'ubuntu-2604-lab-identity-v1', 'almalinux-9-lab-identity-v1', 'almalinux-10-lab-identity-v1'} or mac!=row.mac_address
            or row.provisioning_binding!=ownership.binding_sha256):
        raise Conflict('GUEST_INITIALIZATION_BINDING_CHANGED')
    return instance,target


def request_initialization(session, instance, principal, key, publish, *, current_profiles=True):
    from .authorization import authorize_operation
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
                from .technical_ownership import record as technical_record
                ownership=technical_record(session, instance, adopt=True)
                mac=(instance.result or {}).get('network',{}).get('mac_address','').lower()
                if not re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',mac):raise Conflict('GUEST_INITIALIZATION_MAC_REQUIRED')
                operation=Operation(id=str(uuid4()),tenant_id=instance.tenant_id,instance_id=instance.id,
                    kind='instance.network.initialize',status='queued',idempotency_key=key,request_hash=digest,requested_by=principal.subject)
                session.add(operation);session.flush()
                from .authorization import member
                profile=member(session,instance,'start').profile_snapshots[instance.network_profile]
                from .contracts import instance_uses_technical_access
                technical = instance_uses_technical_access(instance)
                row=GuestInitialization(operation_id=operation.id,instance_id=instance.id,mode=mode,mac_address=mac,
                    program_sha256=(technical_program_for_mode(mode)[1] if technical else
                        program_for_mode(mode, root=bool(profile.get('root_password_bootstrap')))[1]),
                    provisioning_binding=ownership.binding_sha256,status='queued')
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

    def require_initializer_capabilities(self, target):
        """Fail closed when the guest agent intentionally blocks the initializer.

        AlmaLinux images may ship qemu-ga with an allow-list that leaves
        ``guest-exec`` and ``guest-file-read`` disabled.  Treat that as a
        terminal image-capability defect, not as an unknown network result:
        the initializer cannot be consumed safely and must never be replayed.
        Transport/API failures remain ``POWER_PROXMOX_UNAVAILABLE`` so the
        existing uncertain/reconciliation fence is preserved.
        """
        info = self.client._request('GET', target.path + '/agent/info')
        commands = info.get('result', {}).get('supported_commands') if isinstance(info, dict) else None
        enabled = {
            command.get('name') for command in commands
            if isinstance(command, dict) and command.get('enabled') is True
        } if isinstance(commands, list) else set()
        if not GUEST_INITIALIZER_COMMANDS <= enabled:
            raise GuestProgramFailed('GUEST_AGENT_COMMANDS_DISABLED')

    def ready(self,target):
        self.client._request('GET',target.path+'/agent/info')

    def submit(self,target,instance_id,mac):
        return self._submit(target,instance_id,mac,PROGRAM,PROGRAM_SHA256)

    def submit_image(self,target,instance_id,mac,mode):
        program,digest=program_for_mode(mode)
        return self._submit(target,instance_id,mac,program,digest)

    def submit_root(self,target,instance_id,mac):
        return self._submit(target,instance_id,mac,ROOT_PROGRAM,ROOT_PROGRAM_SHA256)

    def submit_technical_access(self,target,instance_id,mac,mode,password,public_keys):
        program,digest=technical_program_for_mode(mode)
        payload=json.dumps({'password':password,'ssh_public_keys':public_keys},
                           sort_keys=True,separators=(',',':'))
        return self._submit(target,instance_id,mac,program,digest,input_data=payload)

    def _submit(self,target,instance_id,mac,program,digest,input_data=None):
        assert str(UUID(instance_id))==instance_id and re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',mac)
        # PVE array form encoding: repeated command parameters, never shell quoting.
        parameters=[
            ('command','/usr/bin/python3'),('command','-c'),('command',program),
            ('command',instance_id),('command',mac),('command',digest)]
        if input_data is not None:
            parameters.append(('input-data',input_data))
        result=self.client._request('POST',target.path+'/agent/exec',parameters)
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
        from .guest_image_programs import NETWORK_MANAGER_FILE
        network_file=NETWORK_MANAGER_FILE if row.mode.startswith('almalinux-') else NETWORK_FILE
        if type(evidence.get('ifindex')) is not int or not 1<=evidence['ifindex']<=65535:
            raise Conflict('GUEST_INITIALIZATION_EVIDENCE_INVALID')
        if (evidence.get('instance_id')!=row.instance_id or evidence.get('mac_address')!=row.mac_address
                or evidence.get('mode')!=row.mode or evidence.get('program_sha256')!=row.program_sha256
                or evidence.get('machine_id_sha256')!=hashlib.sha256(read('/etc/machine-id').encode()).hexdigest()
                or evidence.get('network_sha256')!=hashlib.sha256(read(network_file).encode()).hexdigest()):
            raise Conflict('GUEST_INITIALIZATION_EVIDENCE_INVALID')
        from .network import agent_addresses
        network=agent_addresses(self.client._request('GET',target.path+'/agent/network-get-interfaces'),row.mac_address)
        if not network['ip_addresses']:return None
        if row.mode.startswith('almalinux-'):
            if evidence.get('network_manager_dhcp') is not True:
                raise Conflict('GUEST_INITIALIZATION_EVIDENCE_INVALID')
            return {**evidence,'network':network,'source':'qemu-guest-agent','guest_ready':None}
        try:lease_content=read('/run/systemd/netif/leases/'+str(evidence['ifindex']))
        except PowerError:return None  # networkd replaces the lease while DHCP converges.
        lease=dict(line.split('=',1) for line in lease_content.splitlines() if '=' in line)
        clientid=lease.get('CLIENTID','').lower().replace(':','')
        if clientid!='01'+row.mac_address.replace(':','') or lease.get('ADDRESS') not in network['ip_addresses']:return None
        return {**evidence,'network':network,'source':'qemu-guest-agent','guest_ready':None}


def execute_initialization(identifier,sessions,client_factory=GuestClient.configured,timeout=210):
    cleanup_reference = None
    with sessions() as s:
        row=s.get(GuestInitialization,identifier)
        if row is None or row.status == 'failed':return
        instance_id=row.instance_id
        if row.status == 'completed':
            instance=s.get(Instance, instance_id)
            cleanup_reference=getattr(instance, 'technical_access_reference', None)
            if not cleanup_reference:
                return
    if cleanup_reference:
        _cleanup_technical_access_reference(instance_id, cleanup_reference, sessions)
        return
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
                    from .contracts import instance_uses_technical_access
                    technical = instance_uses_technical_access(instance)
                    technical_password = None
                    client=client_factory()
                    # A freshly started clone can report its QEMU process as
                    # running before Proxmox exposes a stable config/status
                    # pair through the Guest Agent API.  Treat that narrow
                    # transport/readiness window as transient; do not consume
                    # the one-shot guest-exec fence until the binding and
                    # capabilities are observed successfully.
                    deadline=time.monotonic()+timeout
                    while True:
                        try:
                            client.check(target,row.mac_address)
                            capability_check = getattr(client, 'require_initializer_capabilities', None)
                            if capability_check is not None:
                                capability_check(target)
                            break
                        except PowerError:
                            if time.monotonic()>=deadline:raise
                            heartbeat();time.sleep(2)
                    # A capability-repair deployment may encounter an older
                    # consumed fence whose QGA POST failed before returning a
                    # PID.  Rebind only that narrowly evidenced AlmaLinux
                    # observation to the current fixed program; never reopen
                    # a fence with a PID or repeat Terraform/apply.
                    if (row.started_at and row.pid is None
                            and operation.error_code == 'POWER_PROXMOX_UNAVAILABLE'
                            and row.mode.startswith('almalinux-')):
                        current_digest = program_for_mode(row.mode)[1]
                        if row.program_sha256 != current_digest:
                            row.program_sha256 = current_digest
                            event(s,operation,'guest_initialization_program_rebound_after_capability_repair','worker:iac')
                            s.commit()
                    if not row.started_at:
                        configured_mode(s,instance)
                        from ..lifecycle.ownership import mutation_revision
                        mutation_revision()
                        expected_program = (technical_program_for_mode(row.mode)[1] if technical else
                                            program_for_mode(row.mode)[1])
                        if row.program_sha256 not in {expected_program, ROOT_PROGRAM_SHA256 if row.mode==MODE and not technical else expected_program}:
                            raise Conflict('GUEST_INITIALIZATION_PROGRAM_CHANGED')
                        while True:
                            try:client.ready(target);break
                            except PowerError:
                                if time.monotonic()>=deadline:raise Conflict('GUEST_AGENT_UNAVAILABLE') from None
                                heartbeat();time.sleep(2)
                        # Revalidate immediately before consuming the only POST fence.
                        s.refresh(instance);configured_mode(s,instance);mutation_revision();client.check(target,row.mac_address)
                        if technical:
                            from .bootstrap_secrets import EphemeralSecrets
                            technical_password = EphemeralSecrets.configured().read(
                                instance.technical_access_reference,
                                instance.tenant_id, instance.id)
                        row.started_at=utcnow();row.status=operation.status=job.status='running'
                        event(s,operation,'guest_initialization_dispatch_consumed','worker:iac');s.commit()
                        if technical:
                            from .bootstrap_secrets import EphemeralSecrets
                            row.pid=client.submit_technical_access(
                                target, instance.id, row.mac_address, row.mode, technical_password,
                                list(instance.technical_access_public_keys or []))
                        else:
                            submit = client.submit_root if row.program_sha256 == ROOT_PROGRAM_SHA256 else client.submit
                            row.pid=(submit(target,instance.id,row.mac_address) if row.mode==MODE else
                                client.submit_image(target,instance.id,row.mac_address,row.mode))
                        s.commit()
                    while True:
                        validate_binding(s,row,operation)
                        result=client.observe(target,row)
                        if result:
                            if technical:
                                from .access import AccessVerifier
                                if technical_password is None:
                                    from .bootstrap_secrets import EphemeralSecrets
                                    technical_password = EphemeralSecrets.configured().read(
                                        instance.technical_access_reference,
                                        instance.tenant_id, instance.id)
                                access_result = AccessVerifier().verify(
                                    target, instance, password=technical_password,
                                    ip_addresses=result['network']['ip_addresses'])
                                if access_result.get('ssh_authenticated') is not True:
                                    raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID')
                                result = {**result, 'technical_access': {
                                    'password_configured': True,
                                    'ssh_authenticated': True,
                                }}
                            others=s.scalars(select(Instance).where(Instance.id!=instance.id,Instance.status!='deleted',
                                Instance.network_mode=='dhcp')).all()
                            if any(set(result['network']['ip_addresses']) & set(other.ip_addresses or []) for other in others):
                                raise Conflict('DHCP_ADDRESS_CONFLICT')
                            row.result=result;row.status=operation.status=job.status='completed';row.finished_at=utcnow()
                            operation.error_code=None
                            from .network import _observed_observation
                            instance.network_observation=_observed_observation(instance,{
                                **result['network'],'observed_at':utcnow().isoformat(),
                                'identity_initialization':'completed','initialization_operation_id':identifier})
                            instance.ip_addresses=instance.network_observation['ip_addresses']
                            if technical:
                                instance.access_verification={
                                    'status':'completed','cloud_init_complete':True,
                                    'ssh_authenticated':True,
                                    'observed_at':utcnow().isoformat(),
                                }
                                cleanup_reference=instance.technical_access_reference
                            event(s,operation,'guest_initialization_verified','worker:iac');s.commit();break
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
    if cleanup_reference:
        _cleanup_technical_access_reference(instance_id, cleanup_reference, sessions)


def _cleanup_technical_access_reference(instance_id, reference, sessions):
    """Limpia la referencia solo después del commit durable y permite reintento."""
    # Redis es un efecto externo: primero se confirma la evidencia durable en
    # PostgreSQL y luego se elimina la referencia. Si Redis no responde, la
    # referencia permanece con TTL y una entrega posterior puede reintentarlo
    # sin volver a enviar credenciales.
    from .bootstrap_secrets import EphemeralSecrets
    store = EphemeralSecrets.configured()
    store.delete(reference)
    with sessions() as cleanup:
        current = cleanup.get(Instance, instance_id, with_for_update=True)
        if current and current.technical_access_reference == reference:
            current.technical_access_reference = None
            from ..models import CommercialIntent
            intent = cleanup.get(CommercialIntent, instance_id, with_for_update=True)
            if intent and intent.secret_reference == reference:
                intent.secret_reference = None
                intent.bootstrap_status = 'not_applicable'
            cleanup.commit()
