#!/usr/bin/env python3
"""Certificación LAB explícita de Ubuntu por HTTPS.

La ejecución es secuencial, captura inventario histórico antes/después y se
detiene ante cualquier operación global no terminal. Cada VM que crea queda
registrada por UUID y solo esos UUID pueden ser administrados por la corrida.
AlmaLinux permanece suspendido en este runner hasta una certificación separada.
"""
import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

BASE='https://iac.cyberfuel.com'
WORKER='iacp-controlplane-terraform-worker-1'
TOKEN=''
PASSWORD=''
OUT=None
LAST_RESPONSE_IDEMPOTENCY_KEY=None
LAST_ERROR=None
BASELINE_INVENTORY=None
CREATED_INSTANCE_IDS=set()

SUPPORTED_E2E_IMAGES=frozenset({'ubuntu-2404','ubuntu-2604'})
PILOT_NAME_PREFIX={'ubuntu-2404':'prueba-kvm-2404','ubuntu-2604':'prueba-kvm-2604'}
NON_TERMINAL_OPERATION_STATUSES=frozenset({'queued','running','uncertain'})
FATAL_WAIT_ERROR_PREFIXES=(
    'GLOBAL_NON_TERMINAL_OPERATION_BLOCKS_E2E',
    'OPERATION_',
    'FULFILLMENT_',
    'INSTANCE_UNCERTAIN',
)
INVENTORY_IDENTITY_FIELDS=(
    'instance_id','tenant_id','name','image','vmid','node','vcpus',
    'memory_mb','disk_gb','network_mode','network_profile','executor',
    'infrastructure_kind',
)


def _operation_status(row):
    return row.get('normalized_status',row.get('status'))


def _safe_instance_record(row):
    """Conserva inventario útil sin copiar access/commercial_context ni secretos."""
    record={key:row.get(key) for key in (
        'instance_id','tenant_id','name','image','vmid','node','vcpus',
        'memory_mb','disk_gb','network_mode','network_profile','executor',
        'infrastructure_kind','status','operation_id','job_id',
        'created_at','updated_at',
    )}
    observation=row.get('network_observation') or {}
    record.update({
        'ip_addresses':list(row.get('ip_addresses') or []),
        'network_observation_status':observation.get('status'),
        'current_ip_verified':observation.get('current_ip_verified'),
    })
    return record


def _safe_operation_record(row):
    return {
        key:row.get(key) for key in (
            'operation_id','instance_id','kind','status','normalized_status',
            'error_code','recovery_required','created_at','updated_at',
        )
    }


def _list_collection(path,params=None):
    params=dict(params or {})
    offset=0
    rows=[]
    while True:
        query=dict(params,limit=100,offset=offset)
        payload=api(path+'?'+urlencode(query))[1]
        if not isinstance(payload,list):
            raise RuntimeError('API_COLLECTION_NOT_LIST:'+path)
        rows.extend(payload)
        if len(payload)<100:
            return rows
        offset+=len(payload)


def list_instance_inventory():
    return [_safe_instance_record(row) for row in _list_collection('/v1/instances')]


def list_non_terminal_operations():
    rows=[]
    for status in sorted(NON_TERMINAL_OPERATION_STATUSES):
        rows.extend(_list_collection('/v1/operations',{'status':status}))
    unique={row.get('operation_id'):row for row in rows if row.get('operation_id')}
    return [_safe_operation_record(row) for row in unique.values()
            if _operation_status(row) in NON_TERMINAL_OPERATION_STATUSES]


def inventory_scope_diff(before,after,created_ids):
    before_by_id={row['instance_id']:row for row in before}
    after_by_id={row['instance_id']:row for row in after}
    baseline_ids=set(before_by_id)
    after_ids=set(after_by_id)
    missing_baseline=sorted(baseline_ids-after_ids)
    unexpected_after=sorted(after_ids-baseline_ids-set(created_ids))
    changed=[]
    for instance_id in sorted(baseline_ids & after_ids):
        previous=before_by_id[instance_id]
        current=after_by_id[instance_id]
        differences={
            field:(previous.get(field),current.get(field))
            for field in INVENTORY_IDENTITY_FIELDS
            if previous.get(field)!=current.get(field)
        }
        if differences:
            changed.append({'instance_id':instance_id,'fields':differences})
    return {
        'baseline_count':len(before),
        'final_count':len(after),
        'created_instance_ids':sorted(created_ids),
        'missing_baseline_instance_ids':missing_baseline,
        'unexpected_instance_ids':unexpected_after,
        'baseline_identity_changes':changed,
        'scope_ok':not (missing_baseline or unexpected_after or changed),
    }


def assert_no_global_mutations():
    blocking=list_non_terminal_operations()
    if blocking:
        raise RuntimeError(
            'GLOBAL_NON_TERMINAL_OPERATION_BLOCKS_E2E:'+
            json.dumps(blocking,ensure_ascii=False,sort_keys=True,default=str)
        )
    return blocking


def _must_abort_guest_wait(error):
    return str(error).startswith(FATAL_WAIT_ERROR_PREFIXES)


def validate_storage(guest,disk_gb):
    assert guest['disk_bytes']==disk_gb*1024**3
    assert 0<=guest['disk_bytes']-guest['root_partition_end_bytes']<=4*1024**2
    assert 0<=guest['root_partition_bytes']-guest['filesystem_raw_bytes']<guest['filesystem_block_bytes']


def save(name,data):
    raw=json.dumps(data,ensure_ascii=False,indent=2,default=str)
    if (TOKEN and TOKEN in raw) or (PASSWORD and PASSWORD in raw):
        raise RuntimeError('SECRET_IN_RESPONSE')
    (OUT/(name+'.json')).write_text(raw+'\n')


def worker(code,payload=None):
    program='P=json.loads('+repr(json.dumps(payload or {}))+')\n'
    result=subprocess.run(['docker','exec','-i',WORKER,'python','-'],
        input='import json\n'+program+code,text=True,capture_output=True,timeout=120)
    if result.returncode:raise RuntimeError('WORKER_PROBE_FAILED')
    return json.loads(result.stdout)


def api(path,method='GET',body=None,key=None,basic=False):
    global LAST_RESPONSE_IDEMPOTENCY_KEY,LAST_ERROR
    LAST_RESPONSE_IDEMPOTENCY_KEY=None
    LAST_ERROR=None
    headers={'Authorization':('Basic '+base64.b64encode(('iacp:'+TOKEN).encode()).decode()) if basic else 'Bearer '+TOKEN}
    if key:headers['Idempotency-Key']=key
    if body is not None:headers['Content-Type']='application/json'
    request=Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
    try:
        with urlopen(request,timeout=45) as response:
            raw=response.read()
            if PASSWORD and PASSWORD.encode() in raw:raise RuntimeError('SECRET_IN_RESPONSE')
            LAST_RESPONSE_IDEMPOTENCY_KEY=response.headers.get('Idempotency-Key')
            return response.status,json.loads(raw),response.headers.get('X-Request-ID')
    except HTTPError as error:
        raw=error.read()
        try:
            payload=json.loads(raw)
        except (ValueError,TypeError):
            payload={}
        LAST_RESPONSE_IDEMPOTENCY_KEY=error.headers.get('Idempotency-Key')
        LAST_ERROR={'status':error.code,'detail':payload.get('detail'),'idempotency_key':LAST_RESPONSE_IDEMPOTENCY_KEY}
        raise RuntimeError('API_HTTP_'+str(error.code)+(':'+str(payload.get('detail')) if payload.get('detail') else '')) from None


def lifecycle_intent(instance_id, path, method, body, label, explicit, key=None):
    """Ejecuta una mutación con una intención estable y recupera INSTANCE_BUSY.

    En modo generado la primera petición omite el header; IACP devuelve la clave
    interna y los reintentos de esa misma intención la reutilizan. En modo
    explícito el runner genera la clave antes del primer envío. Nunca se crea una
    clave nueva dentro del bucle de reintento.
    """
    assert_no_global_mutations()
    intent_key=key if explicit else None
    for attempt in range(1,7):
        try:
            status,operation,request_id=api(path,method,body,intent_key)
            resolved=LAST_RESPONSE_IDEMPOTENCY_KEY
            if not resolved:
                raise RuntimeError('IDEMPOTENCY_KEY_NOT_RETURNED')
            return status,operation,request_id,resolved,attempt
        except RuntimeError as error:
            busy=LAST_ERROR and LAST_ERROR.get('status')==409 and 'INSTANCE_BUSY' in str(LAST_ERROR.get('detail'))
            if not busy or attempt==6:
                raise
            if intent_key is None:
                intent_key=LAST_RESPONSE_IDEMPOTENCY_KEY
            if not intent_key:
                raise RuntimeError('INSTANCE_BUSY_WITHOUT_IDEMPOTENCY_KEY') from error
            wait_instance_idle(instance_id)
            time.sleep(1)
    raise RuntimeError('INSTANCE_BUSY_RETRY_EXHAUSTED')


def wait_operation(identifier,fulfillment=False,allow_reconcile=False):
    deadline=time.monotonic()+900
    while time.monotonic()<deadline:
        _,row,_=api('/v1/operations/'+identifier)
        if (allow_reconcile and row.get('recovery_required')
                and (row.get('provisioning') or {}).get('phase')=='uncertain'):
            _,response,_=api('/v1/instances/'+row['instance_id']+'/reconcile','POST',{})
            save('reconciliacion-'+identifier,{'antes':row,'respuesta':response,'alcance':'Observación sin segundo apply'})
            time.sleep(2);continue
        status=row.get('normalized_status',row['status'])
        if status in {'failed','uncertain','cancelled'}:
            save('operacion-fallida-'+identifier,row)
            raise RuntimeError('OPERATION_'+row['status'].upper())
        progress=row.get('fulfillment') or {}
        if progress.get('status') in {'failed','uncertain'}:
            save('operacion-fallida-'+identifier,row)
            raise RuntimeError('FULFILLMENT_'+progress['status'].upper())
        if status=='completed' and (not fulfillment or progress.get('phase')=='verified'):
            return row
        time.sleep(5)
    save('operacion-tiempo-agotado-'+identifier,row)
    raise RuntimeError('OPERATION_TIMEOUT_NO_RETRY')


def wait_instance_idle(identifier):
    deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        rows=api('/v1/operations?instance_id='+identifier+'&limit=100')[1]
        if any(r.get('normalized_status',r['status'])=='uncertain' for r in rows):raise RuntimeError('INSTANCE_UNCERTAIN')
        if not any(r.get('normalized_status',r['status']) in {'queued','running'} for r in rows):return rows
        time.sleep(2)
    raise RuntimeError('INSTANCE_OPERATIONS_TIMEOUT')


def wait_guest(data):
    deadline=time.monotonic()+180
    discoveries=0
    while time.monotonic()<deadline:
        try:
            instance=api('/v1/instances/'+data['instance_id'])[1]
            if not instance.get('ip_addresses') and discoveries<3:
                assert_no_global_mutations()
                _,op,_=api('/v1/instances/'+data['instance_id']+'/network/discover','POST',{},
                    'e2e-g-network-'+secrets.token_hex(12))
                save('network-discovery-'+op['operation_id'],wait_operation(op['operation_id']))
                discoveries+=1
            return worker(PROBE,{**data,'without_password':True})
        except RuntimeError as error:
            if _must_abort_guest_wait(error):
                raise
            time.sleep(3)
    raise RuntimeError('GUEST_READINESS_TIMEOUT')


PROBE=r'''
import base64,hashlib,os,paramiko,tempfile,time
from pathlib import Path
from urllib.parse import urlencode
from iacp.db import get_session_factory
from iacp.models import Instance,TerraformExecution,CommercialIntent
from iacp.services.guest_identity import GuestClient
from iacp.lifecycle.proxmox import Target
from iacp.terraform.config import Allocation
from iacp.terraform.lifecycle import build_executor
from iacp.terraform.observation import proxmox_resource
from iacp.terraform.state import S3StateReader
from iacp.terraform.executor import state_resource
with get_session_factory()() as s:
 i=s.get(Instance,P['instance_id']);e=s.get(TerraformExecution,i.id);c=s.get(CommercialIntent,i.id)
 assert c.plan_id==P['plan_id'] and i.image==P.get('image_id','ubuntu-2404') and e.vmid>=30000
 assert e.vmid not in (104,106,29000,29001,29010,29011)
 a=Allocation.model_validate(e.allocation);v=a.variables(i)
client=GuestClient.configured().client
target=Target(e.vmid,a.node_name,i.name)
with tempfile.TemporaryDirectory() as tmp:
 env=build_executor(observation=True).environment(Path(tmp))
 observation=proxmox_resource(env,v['instance'],lambda:False)
 reader=S3StateReader(a.backend.arguments(e.state_key),env)
 try:
  state=reader.read(lambda:False);resource=state_resource(state.state,v)
  if P.get('password'):assert P['password'] not in json.dumps(state.state)
  state_result={'present':resource is not None,'metadata':state.metadata()}
  if P.get('deleted'):
   from botocore.exceptions import ClientError
   try:
    reader.client.head_object(Bucket=reader.bucket,Key=reader.key+'.tflock')
    raise AssertionError('STATE_LOCK_PRESENT')
   except ClientError as failure:
    assert failure.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==404
    state_result['lock_absent']=True
 finally:reader.close()
result={'vmid':e.vmid,'node':a.node_name,'instance_status':i.status,'bootstrap_status':c.bootstrap_status,
        'secret_reference_removed':c.secret_reference is None,'proxmox':observation,'state':state_result}
if P.get('deleted'):
 assert i.status=='deleted' and not observation['present'] and resource is None
 stores={a.boot_disk.datastore_id,a.cloudinit_datastore_id,a.clone_datastore_id}-{None}
 residual=[]
 for storage in sorted(stores):
  for entry in client._request('GET',f'/nodes/{a.node_name}/storage/{storage}/content'):
   if str(entry.get('vmid',''))==str(e.vmid) or ('vm-'+str(e.vmid)+'-') in entry.get('volid',''):
    residual.append(entry['volid'])
 assert not residual
 result['residual_disks']=residual
else:
 assert c.bootstrap_status=='completed' and c.secret_reference is None and observation['present'] and resource
 cfg=client._request('GET',target.path+'/config')
 from iacp.terraform.golden_hardware import verify_live,verify_cpu
 from iacp.terraform.executor import verify_plan
 hardware=v['iacp_golden_hardware']
 verify_live(cfg,hardware,v['instance']);verify_cpu(resource,v)
 executor=build_executor(observation=True)
 saved=executor.workspace(i.id,e.attempt)/'source/stacks/kvm-instance'
 plan=executor.read_json(['show','-json','saved.tfplan'],saved,executor.environment(executor.workspace(i.id,e.attempt)),lambda:False,'e2e.inspect')
 verify_plan(plan,v)
 result['golden_hardware']={'source':hardware,'terraform_plan_cpu':plan['resource_changes'][0]['change']['after']['cpu'],
  'effective':{k:cfg.get(k) for k in ['cpu','numa','sockets','cores','bios','machine','scsihw','hotplug','boot']},'status':'PASS'}
 assert int(cfg['cores'])*int(cfg.get('sockets',1))==P['cpus'] and int(cfg['memory'])==P['memory_mb']
 result['proxmox_resources']={'cores':cfg['cores'],'memory_mb':cfg['memory'],'boot_disk':cfg[a.boot_disk.interface],'cloud_init_network':cfg.get('ipconfig0')}
 host=client._request('GET',target.path+'/agent/file-read?'+urlencode({'file':'/etc/ssh/ssh_host_ed25519_key.pub','count':8192}))['content'].split()
 key=paramiko.PKey.from_path(os.environ['IACP_GUEST_BOOTSTRAP_SSH_KEY_FILE'])
 address=i.ip_addresses[0]
 with paramiko.SSHClient() as ssh:
  ssh.get_host_keys().add(address,'ssh-ed25519',paramiko.Ed25519Key(data=base64.b64decode(host[1])))
  ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
  ssh.connect(address,username='root',pkey=key,allow_agent=False,look_for_keys=False,timeout=15)
  import shlex
  program="""
import json,os,subprocess,re
from pathlib import Path
def output(args):return subprocess.check_output(args,text=True).strip()
root=str(Path(output(['findmnt','-n','-o','SOURCE','/'])).resolve())
disk='/dev/'+output(['lsblk','-n','-o','PKNAME',root])
table=json.loads(output(['sfdisk','--json',disk]))['partitiontable']
partition=next(p for p in table['partitions'] if p['node']==root)
filesystem=output(['findmnt','-n','-o','FSTYPE','/'])
if filesystem=='ext4':
 header=output(['dumpe2fs','-h',root]);block=int(re.search(r'^Block size:\\s+(\\d+)',header,re.M).group(1))
 blocks=int(re.search(r'^Block count:\\s+(\\d+)',header,re.M).group(1))
elif filesystem=='xfs':
 info=output(['xfs_info','/'])
 block,blocks=map(int,re.search(r'^data\\s+=\\s+bsize=(\\d+)\\s+blocks=(\\d+)',info,re.M).groups())
else:raise RuntimeError('Filesystem no compatible con la sonda: '+filesystem)
sector=table['sectorsize']
v=os.statvfs('/');r=subprocess.run(['cloud-init','status','--format','json'],capture_output=True)
c=json.loads(r.stdout)
assert r.returncode in (0,2) and not c.get('errors') and set(c.get('recoverable_errors',{}))<={'DEPRECATED'}
release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
print(json.dumps({'os_id':release['ID'].strip('\"'),'os_version':release['VERSION_ID'].strip('\"'),'filesystem':filesystem,'root_fs_bytes':v.f_frsize*v.f_blocks,'cloud_init':c.get('status'),
 'cloud_init_rc':r.returncode,'cloud_init_warnings':c.get('recoverable_errors',{}),'cpus':os.cpu_count(),
 'disk_bytes':int(output(['blockdev','--getsize64',disk])),
 'root_partition_bytes':partition['size']*sector,'root_partition_end_bytes':(partition['start']+partition['size'])*sector,
 'filesystem_raw_bytes':blocks*block,'filesystem_block_bytes':block,
 'lsblk':json.loads(output(['lsblk','-b','-J','-o','NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS']))}))
"""
  cmd='LC_ALL=C python3 -c '+shlex.quote(program)
  _,stdout,_=ssh.exec_command(cmd,timeout=30);guest=json.loads(stdout.read());assert stdout.channel.recv_exit_status()==0
  assert guest['cpus']==P['cpus'] and guest['cloud_init']=='done'
  expected={'ubuntu-2404':('ubuntu','24.04'),'ubuntu-2604':('ubuntu','26.04'),'almalinux-9':('almalinux','9'),'almalinux-10':('almalinux','10')}[P.get('image_id','ubuntu-2404')]
  assert guest['os_id']==expected[0] and (guest['os_version']==expected[1] if expected[0]=='ubuntu' else guest['os_version'].split('.')[0]==expected[1])
  assert guest['disk_bytes']==P['disk_gb']*1024**3
  assert 0<=guest['disk_bytes']-guest['root_partition_end_bytes']<=4*1024**2
  assert 0<=guest['root_partition_bytes']-guest['filesystem_raw_bytes']<guest['filesystem_block_bytes']
  assert any(r['type']=='disk' and int(r['size'])==P['disk_gb']*1024**3 for r in guest['lsblk']['blockdevices'])
  result.update(guest=guest,ip=address,ssh_root_key=True)
  _,policy,_=ssh.exec_command('sshd -T -C user=root,host=localhost,addr=127.0.0.1',timeout=15)
  text=policy.read().decode();assert policy.channel.recv_exit_status()==0
  assert 'permitrootlogin yes\n' in text and 'passwordauthentication yes\n' in text
  result['root_ssh_password_enabled']=True
  if P.get('without_password'):
   result['root_password_pam']='NO_EJECUTADO_CLIENTE_SIN_SECRETO'
   print(json.dumps(result,default=str));raise SystemExit(0)
  with paramiko.SSHClient() as password_ssh:
   password_ssh.get_host_keys().add(address,'ssh-ed25519',paramiko.Ed25519Key(data=base64.b64decode(host[1])))
   password_ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
   password_ssh.connect(address,username='root',password=P['password'],allow_agent=False,look_for_keys=False,timeout=15,auth_timeout=15)
   _,identity,_=password_ssh.exec_command('id -u',timeout=10)
   assert identity.read().strip()==b'0' and identity.channel.recv_exit_status()==0
  result['ssh_root_password']=True
  channel=ssh.get_transport().open_session();channel.get_pty()
  channel.exec_command("stty -echo; LC_ALL=C runuser -u nobody -- su - root -c 'id -u'")
  received=b'';deadline=time.monotonic()+30
  while b'Password:' not in received and time.monotonic()<deadline:
   if channel.recv_ready():received+=channel.recv(4096)
   elif channel.exit_status_ready():break
   else:time.sleep(.1)
  assert b'Password:' in received
  channel.sendall((P['password']+'\n').encode())
  while not channel.exit_status_ready() and time.monotonic()<deadline:time.sleep(.1)
  assert channel.exit_status_ready() and channel.recv_exit_status()==0
  result['root_password_pam']=True;channel.close()
 for file in (Path('/var/lib/iacp/terraform')/i.id).rglob('*'):
  if file.is_file():
   with file.open('rb') as stream:
    previous=b''
    while block:=stream.read(1024*1024):
     chunk=previous+block;assert P['password'].encode() not in chunk
     previous=chunk[-len(P['password']):]
 from sqlalchemy import MetaData,select
 from iacp.db import get_engine
 with get_engine().connect() as connection:
  metadata=MetaData();metadata.reflect(connection)
  for table in metadata.tables.values():
   for row in connection.execute(select(table)).mappings():
    assert P['password'] not in json.dumps(dict(row),default=str)
 from redis import Redis
 from iacp.config import broker_url
 redis=Redis.from_url(broker_url());needle=P['password'].encode()
 for rediskey in redis.scan_iter():
  assert needle not in rediskey
  kind=redis.type(rediskey)
  values=(redis.lrange(rediskey,0,-1) if kind==b'list' else
    list(redis.hgetall(rediskey).values()) if kind==b'hash' else
    list(redis.smembers(rediskey)) if kind==b'set' else
    redis.zrange(rediskey,0,-1) if kind==b'zset' else
    [redis.get(rediskey)] if kind==b'string' else [])
  assert all(needle not in value for value in values if isinstance(value,bytes))
 result['password_absent_from_state_and_artifacts']=True
 result['password_absent_from_postgresql_and_redis']=True
print(json.dumps(result,default=str))
'''


def main():
    global TOKEN,PASSWORD,OUT,BASELINE_INVENTORY,CREATED_INSTANCE_IDS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true');parser.add_argument('--evidence-dir',type=Path,required=True)
    parser.add_argument('--resume-instance');parser.add_argument('--accepted-file',type=Path)
    parser.add_argument('--plan',choices=['kvm-1','kvm-2','kvm-3','kvm-4'])
    parser.add_argument('--image',choices=sorted(SUPPORTED_E2E_IMAGES),default='ubuntu-2404')
    parser.add_argument('--token-file',type=Path)
    parser.add_argument('--minimal',action='store_true',help='Cuerpo mínimo del piloto; exige token de integración explícito')
    parser.add_argument('--idempotency-mode',choices=['generated','explicit'],default='generated',
        help='generated omite Idempotency-Key en el primer envío; explicit envía una clave del runner')
    args=parser.parse_args()
    if not args.execute:print('Preparado: requiere --execute explícito; no se ejecutaron solicitudes.');return
    assert socket.gethostname()=='iacp-controlplane-01'
    os.umask(0o077);OUT=args.evidence_dir;OUT.mkdir(mode=0o700,parents=True,exist_ok=False)
    if args.image not in SUPPORTED_E2E_IMAGES:
        raise RuntimeError('ALMALINUX_E2E_SUSPENDIDO')
    if args.resume_instance or args.accepted_file:
        raise RuntimeError('RESUME_INSTANCE_NOT_ALLOWED_IN_ISOLATED_E2E')
    explicit_idempotency=args.idempotency_mode=='explicit'
    containers=json.loads(subprocess.check_output(['docker','inspect','iacp-controlplane-api-1'],text=True))
    assert not args.minimal or args.token_file, 'El piloto requiere identidad de integración explícita'
    tokenpath=args.token_file or next(m['Source'] for m in containers[0]['Mounts'] if m['Destination']=='/run/secrets/api_token')
    TOKEN=Path(tokenpath).read_text().strip()
    key=worker("import os,paramiko\nk=paramiko.PKey.from_path(os.environ['IACP_GUEST_BOOTSTRAP_SSH_KEY_FILE']);print(json.dumps({'public':k.get_name()+' '+k.get_base64()}))")['public']
    BASELINE_INVENTORY=list_instance_inventory()
    CREATED_INSTANCE_IDS=set()
    save('inventario-inicial',BASELINE_INVENTORY)
    initial_non_terminal=assert_no_global_mutations()
    save('operaciones-no-terminales-iniciales',initial_non_terminal)
    _,contract,_=api('/openapi.json')
    assert contract['openapi']=='3.1.0' and 'plan_id' in json.dumps(contract['paths']['/v1/instances']['post'])+json.dumps(contract['components']['schemas']['CommercialInstanceCreate'])
    save('openapi',contract);save('planes',api('/v1/plans')[1]);save('imagenes',api('/v1/images')[1])
    run=datetime.now(ZoneInfo('America/Costa_Rica')).strftime('%Y%m%d-%H%M%S')
    sizes=[(1,1,2048,50),(2,2,4096,100),(3,4,8192,200),(4,8,16384,400)]
    for n,cpus,ram,disk in sizes:
        if n>2 and not args.plan:continue
        if args.plan and args.plan!='kvm-'+str(n):continue
        assert_no_global_mutations()
        plan='kvm-'+str(n);PASSWORD=secrets.token_urlsafe(28)+'A9!'
        body={'name':PILOT_NAME_PREFIX[args.image]+'-'+f'{n:03d}','plan_id':plan,'image_id':args.image,
              'network':{'profile_id':'lab-dhcp'},'access':{'root_password':PASSWORD,'ssh_public_keys':[key]},
              'start_on_create':True,'commercial_context':{'order_id':'E2E-G-'+run+'-'+str(n),'product_id':'CRM-KVM-'+str(n)}}
        if args.minimal:
            body.pop('network');body.pop('commercial_context');body.pop('start_on_create')
        if not args.resume_instance:
            available=api('/v1/plans')[1]
            assert next(p for p in available if p['plan_id']==plan)['availability']['available'], 'Plan no disponible; no enviar creación'
            image=next(i for i in api('/v1/images')[1] if i['image_id']==args.image)
            assert image['authorized'] and image['availability']['available'] and plan in image['compatible_plans'], 'Imagen no disponible para este plan'
        idem='e2e-g-'+run+'-'+plan+'-'+args.image
        if n==1 and args.resume_instance:
            recovered=worker("from sqlalchemy import select\nfrom iacp.db import get_session_factory\nfrom iacp.models import CommercialIntent,TerraformExecution,Operation,TerraformApply\nfrom iacp.services.bootstrap_secrets import EphemeralSecrets\nwith get_session_factory()() as s:\n c=s.get(CommercialIntent,P['id']);e=s.get(TerraformExecution,P['id']);o=s.get(Operation,e.operation_id)\n approval=s.scalar(select(TerraformApply).where(TerraformApply.instance_id==c.instance_id,TerraformApply.attempt==e.attempt))\n mode='reconcile' if approval and approval.status=='uncertain' and approval.started_at else 'retry-provision'\n assert c.plan_id=='kvm-1' and c.bootstrap_started_at is None and (mode=='reconcile' or e.status=='failed' and e.error_code=='SOURCE_UNAVAILABLE')\n password=EphemeralSecrets.configured().read(c.secret_reference,o.tenant_id,c.instance_id)\n print(json.dumps({'password':password,'attempt':e.attempt,'key':o.idempotency_key,'mode':mode}))",{'id':args.resume_instance})
            PASSWORD=recovered.pop('password');idem=recovered['key']
            accepted=json.loads(args.accepted_file.read_text())['respuesta']
            assert accepted['instance_id']==args.resume_instance
            mode=recovered['mode']
            status,queued,request_id=api('/v1/instances/'+args.resume_instance+'/'+mode,'POST',
                {'expected_attempt':recovered['attempt']} if mode=='retry-provision' else {},
                idem+'-retry-source-'+str(recovered['attempt']))
            assert status==202 and queued['instance_id']==args.resume_instance
            save(plan+'-retry-plan',{'respuesta':queued,'request_id':request_id,'misma_instancia':True})
        else:
            create_key=idem if explicit_idempotency else None
            status,accepted,request_id=api('/v1/instances','POST',body,create_key);assert status==202
            resolved_create_key=LAST_RESPONSE_IDEMPOTENCY_KEY
            if not resolved_create_key:raise RuntimeError('IDEMPOTENCY_KEY_NOT_RETURNED')
            save(plan+'-aceptada',{'respuesta':accepted,'request_id':request_id,'plan_id':plan,
                'idempotency_mode':args.idempotency_mode,'idempotency_key_returned':resolved_create_key})
            replay=api('/v1/instances','POST',body,resolved_create_key)[1]
            assert replay==accepted
            idem=resolved_create_key
        CREATED_INSTANCE_IDS.add(accepted['instance_id'])
        fixture=worker("from iacp.db import get_session_factory\nfrom iacp.models import Instance\nfrom iacp.services.bootstrap_secrets import EphemeralSecrets\nwith get_session_factory()() as s:\n i=s.get(Instance,P['instance_id']);store=EphemeralSecrets.configured();ref=store.put(i.tenant_id,i.id,P['password'])\n print(json.dumps({'reference':ref,'tenant_id':i.tenant_id,'instance_id':i.id,'ttl_seconds':store.redis.ttl('iacp:bootstrap:v1:'+ref)}))",{'instance_id':accepted['instance_id'],'password':PASSWORD})
        # Fixture cifrada de cliente, independiente de la referencia productiva.
        # Solo referencia opaca en archivo privado; nunca exportar este archivo a Git.
        save(plan+'-client-canary-fixture',fixture)
        save(plan+'-creacion',wait_operation(accepted['operation_id'],fulfillment=True))
        identifier=accepted['instance_id'];data={'instance_id':identifier,'image_id':args.image,'plan_id':plan,'cpus':cpus,'memory_mb':ram,'disk_gb':disk,'password':PASSWORD}
        wait_instance_idle(identifier)
        wait_guest(data)
        save(plan+'-proxmox-guest-state',worker(PROBE,data))
        for index,action in enumerate(['shutdown','start','reboot','shutdown']):
            wait_instance_idle(identifier)
            if action in {'shutdown','reboot'}:wait_guest(data)
            action_key=idem+'-'+str(index)+'-'+action if explicit_idempotency else None
            _,operation,request_id,resolved_action_key,attempts=lifecycle_intent(identifier,
                '/v1/instances/'+identifier+'/power/'+action,'POST',{},action,explicit_idempotency,action_key)
            save(plan+'-'+str(index)+'-'+action,wait_operation(operation['operation_id']))
            replay=api('/v1/instances/'+identifier+'/power/'+action,'POST',{},resolved_action_key)[1]
            assert replay['operation_id']==operation['operation_id']
            if action in {'start','reboot'}:save(plan+'-'+str(index)+'-guest-ready',wait_guest(data))
        save(plan+'-operaciones-antes-delete',wait_instance_idle(identifier))
        delete_key=idem+'-delete' if explicit_idempotency else None
        status,operation,request_id,resolved_delete_key,attempts=lifecycle_intent(identifier,
            '/v1/instances/'+identifier,'DELETE',{},'delete',explicit_idempotency,delete_key)
        assert status==202
        save(plan+'-delete',wait_operation(operation['operation_id']))
        replay=api('/v1/instances/'+identifier,'DELETE',{},resolved_delete_key)[1]
        assert replay['operation_id']==operation['operation_id']
        save(plan+'-limpieza',worker(PROBE,{**data,'deleted':True}))
        for role in ['api','worker','terraform-worker','lifecycle-worker']:
            logs=subprocess.run(['docker','logs','--since','30m','iacp-controlplane-'+role+'-1'],capture_output=True)
            if PASSWORD.encode() in logs.stdout+logs.stderr:raise RuntimeError('SECRET_IN_LOGS')
        cleaned=worker("from iacp.services.bootstrap_secrets import EphemeralSecrets\ns=EphemeralSecrets.configured();s.delete(P['reference']);print(json.dumps({'removed':not s.redis.exists('iacp:bootstrap:v1:'+P['reference'])}))",fixture)
        assert cleaned['removed'];save(plan+'-client-fixture-cleanup',cleaned)
        save(plan+'-resultado',{'estado':'PASS','fecha_local':datetime.now(ZoneInfo('America/Costa_Rica')).isoformat()})
        print(json.dumps({'plan':plan,'estado':'PASS','instance_id':identifier}),flush=True)
    final_inventory=list_instance_inventory()
    save('inventario-final',final_inventory)
    scope=inventory_scope_diff(BASELINE_INVENTORY,final_inventory,CREATED_INSTANCE_IDS)
    save('alcance-inventario',scope)
    if not scope['scope_ok']:
        raise RuntimeError('INVENTORY_SCOPE_CHANGED:'+json.dumps(scope,ensure_ascii=False,sort_keys=True))
    save('resultado',{'estado':'PASS','image_id':args.image,'planes':[args.plan] if args.plan else ['kvm-1','kvm-2'],'fecha_local':datetime.now(ZoneInfo('America/Costa_Rica')).isoformat()})


if __name__=='__main__':
    try:main()
    except Exception as error:
        message=str(error) if isinstance(error,RuntimeError) else type(error).__name__
        if OUT is not None:
            if TOKEN and BASELINE_INVENTORY is not None:
                try:
                    final_inventory=list_instance_inventory()
                    save('inventario-final',final_inventory)
                    save('alcance-inventario',inventory_scope_diff(
                        BASELINE_INVENTORY,final_inventory,CREATED_INSTANCE_IDS))
                except Exception:
                    save('inventario-final',{'estado':'NO_DISPONIBLE'})
            save('resultado',{'estado':'FAIL','motivo':message,'fecha_local':datetime.now(ZoneInfo('America/Costa_Rica')).isoformat()})
        print(json.dumps({'estado':'FAIL','motivo':message,'sin_reintento_de_clonacion':True}),flush=True)
        raise SystemExit(1)
