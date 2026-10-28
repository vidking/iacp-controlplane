"""Read-only Phase C evidence from the existing authorized MNG runtime.

Only allowlisted metadata leaves the host. No environment dump, response body,
credential value, state content or Terraform command is collected.
"""
import argparse
import json
from pathlib import Path
import subprocess


RUNTIME = r'''
import datetime,hashlib,json,os
from pathlib import Path
from sqlalchemy import text,select
from redis import Redis
from iacp.db import get_session_factory
from iacp.worker import celery_app
from iacp.models import Instance,TerraformExecution
from iacp.services.guest_identity import GuestClient
from iacp.terraform.config import Allocation
from iacp.terraform.state import S3StateReader
from iacp.config import broker_url
from kombu.transport.redis import Channel
import iacp.terraform.worker,iacp.lifecycle.worker
result={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
 'celery':{k:celery_app.conf.get(k) for k in ['task_acks_late','task_acks_on_failure_or_timeout','task_reject_on_worker_lost','worker_prefetch_multiplier','broker_transport_options','task_publish_retry','worker_cancel_long_running_tasks_on_connection_loss']},
 'visibility_timeout':Channel.visibility_timeout,
 'tasks':{n:{k:getattr(t,k,None) for k in ['acks_late','reject_on_worker_lost','soft_time_limit','time_limit','max_retries']} for n,t in celery_app.tasks.items() if n.startswith('iacp.')}}
with get_session_factory()() as s:
 s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
 result['schema']=s.execute(text('SELECT version_num FROM alembic_version')).scalar()
 for name,query in {
  'nonterminal_operations':"SELECT id,instance_id,kind,status,error_code FROM operations WHERE status IN ('queued','running','uncertain')",
  'pending_applies':"SELECT id,instance_id,status,error_code FROM terraform_applies WHERE status IN ('approved','queued','running','uncertain')",
  'pending_jobs':"SELECT id,operation,operation_id,status FROM jobs WHERE status IN ('queued','running')",
  'advisory_locks':"SELECT pid,classid,objid,granted FROM pg_locks WHERE locktype='advisory'",
  'db_activity':"SELECT state,wait_event_type,count(*) FROM pg_stat_activity WHERE datname=current_database() GROUP BY state,wait_event_type",
  'policies':"SELECT id,status,expires_at,specification FROM lab_policies WHERE status='approved'"
 }.items():result[name]=[dict(r._mapping) for r in s.execute(text(query))]
 p=GuestClient.configured().client
 inventory=p._request('GET','/cluster/resources?type=vm')
 assert all(v['node'].startswith('mng') for v in inventory)
 result['cluster_nodes']=sorted({v['node'] for v in inventory})
 result['test_vmids_present']=sorted(int(v['vmid']) for v in inventory if int(v['vmid'])>=30000)
 result['protected_hashes']={str(vmid):hashlib.sha256(json.dumps(p._request('GET',f'/nodes/mng01/qemu/{vmid}/config?current=1'),sort_keys=True).encode()).hexdigest() for vmid in [106,29000]}
 result['vm104_effective_permissions']=p._request('GET','/access/permissions?path=/vms/104')
 result['states']=[]
 for i in s.scalars(select(Instance).order_by(Instance.created_at.desc()).limit(4)):
  e=s.get(TerraformExecution,i.id)
  if not e or not e.allocation:continue
  a=Allocation.model_validate(e.allocation)
  # S3 reader receives the existing context in memory; no temporary state is written.
  credentials=json.loads(Path(os.environ['IACP_TERRAFORM_CREDENTIALS_FILE']).read_text())
  ca=os.environ.get('IACP_TERRAFORM_CA_FILE')
  if ca:credentials['SSL_CERT_FILE']=ca
  reader=S3StateReader(a.backend.arguments(e.state_key),credentials)
  try:
   snapshot=reader.read(lambda:False)
   state={'instance_id':i.id,'vmid':e.vmid,'inventory_status':i.status,'state_key':e.state_key,
    'metadata':snapshot.metadata(),'managed_resources':sum(len(r.get('instances',[])) for r in snapshot.state['resources'] if r.get('mode')=='managed')}
   try:reader.client.head_object(Bucket=reader.bucket,Key=reader.key+'.tflock');state['lock_present']=True
   except Exception as error:state['lock_present']=False if getattr(error,'response',{}).get('ResponseMetadata',{}).get('HTTPStatusCode')==404 else None
   result['states'].append(state)
  except Exception as error:result['states'].append({'instance_id':i.id,'error_code':getattr(error,'code',type(error).__name__)})
  finally:reader.close()
with Redis.from_url(broker_url(),socket_timeout=3) as r:
 result['queues']={}
 for key in r.scan_iter(count=100):
  name=key.decode('utf8',errors='replace')
  if name.split('\x06\x16')[0] in {'celery','terraform','lifecycle'}:result['queues'][name]=r.llen(key)
 result['unacked']=r.hlen('unacked');result['unacked_index']=r.zcard('unacked_index')
 info=r.info();result['redis']={k:info.get(k) for k in ['redis_version','connected_clients','used_memory','blocked_clients','aof_enabled','aof_last_write_status']}
 result['redis_appendfsync']=r.config_get('appendfsync').get('appendfsync')
result['worker_ping']=celery_app.control.inspect(timeout=3).ping()
print(json.dumps(result,sort_keys=True,default=str))
'''

PERFORMANCE = r'''
import datetime,hashlib,json,time
from sqlalchemy import event,text,select
from iacp.db import get_session_factory,get_engine
from iacp.models import BOOTSTRAP_TENANT_ID,Instance
from iacp.security import Principal
from iacp.api.operations import list_operations
from iacp.api.power import get_power
from iacp.lifecycle.proxmox import ProxmoxPower
result={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'operations':[]}
principal=Principal(BOOTSTRAP_TENANT_ID,'phase-c-readonly','reader')
engine=get_engine();stats={'queries':0,'sql_ms':0}
def before(conn,cursor,statement,parameters,context,many):
 context.phase_c_start=time.perf_counter()
def after(conn,cursor,statement,parameters,context,many):
 stats['queries']+=1;stats['sql_ms']+=(time.perf_counter()-context.phase_c_start)*1000
event.listen(engine,'before_cursor_execute',before);event.listen(engine,'after_cursor_execute',after)
try:
 for limit in [1,10,50,100]:
  for repeat in range(3):
   with get_session_factory()() as s:
    s.execute(text('SET TRANSACTION READ ONLY'))
    stats.update(queries=0,sql_ms=0)
    start=time.perf_counter();rows=list_operations(principal,s,limit=limit,offset=0)
    total_ms=round((time.perf_counter()-start)*1000,3)
    payload=json.dumps([row.model_dump(mode='json',by_alias=True) for row in rows],sort_keys=True,separators=(',',':')).encode()
    result['operations'].append({'limit':limit,'repeat':repeat,'rows':len(rows),'queries':stats['queries'],'sql_ms':round(stats['sql_ms'],3),'total_ms':total_ms,'payload_sha256':hashlib.sha256(payload).hexdigest()})
 with get_session_factory()() as s:
  s.execute(text('SET TRANSACTION READ ONLY'))
  result['operation_count']=s.scalar(text('SELECT count(*) FROM operations WHERE tenant_id=:t'),{'t':BOOTSTRAP_TENANT_ID})
  result['indexes']=[dict(r._mapping) for r in s.execute(text("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE tablename IN ('operations','operation_events')"))]
  result['query_plan']=s.execute(text('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT * FROM operations WHERE tenant_id=:t ORDER BY created_at,id LIMIT 50'),{'t':BOOTSTRAP_TENANT_ID}).scalar()
  instance=s.scalar(select(Instance).where(Instance.status=='ready',Instance.tenant_id==BOOTSTRAP_TENANT_ID))
  result['power_samples']=[]
  if instance:
   client=ProxmoxPower.configured();original=client._request
   for repeat in range(3):
    calls=[]
    def request(method,path,parameters=None):
     assert method=='GET'
     start=time.perf_counter();value=original(method,path,parameters)
     calls.append({'route':path.split('?')[0],'milliseconds':round((time.perf_counter()-start)*1000,3)})
     return value
    client._request=request
    start=time.perf_counter();value=get_power(instance.id,principal,s,lambda:client)
    result['power_samples'].append({'repeat':repeat,'total_ms':round((time.perf_counter()-start)*1000,3),'provider_calls':calls,'mutations_available':value.mutations_available,'blocked_reason':value.blocked_reason})
finally:
 event.remove(engine,'before_cursor_execute',before);event.remove(engine,'after_cursor_execute',after)
print(json.dumps(result,sort_keys=True,default=str))
'''

HOST = r'''
import datetime,json,os,socket,subprocess
assert socket.gethostname()=='iacp-controlplane-01'
def call(args):return subprocess.check_output(args,text=True)
ids=call(['docker','ps','-q','--filter','label=com.docker.compose.project=iacp-controlplane']).split()
containers=json.loads(call(['docker','inspect',*ids]))
allowed={'IACP_ADMISSION_ENABLED','IACP_INSTANCE_EXECUTOR','IACP_TERRAFORM_PLAN_ENABLED','IACP_TERRAFORM_APPLY_ENABLED','IACP_TERRAFORM_DELETE_ENABLED','IACP_KVM_LIFECYCLE_ENABLED'}
result={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'host':socket.gethostname(),'cpus':os.cpu_count(),'uptime_seconds':float(open('/proc/uptime').read().split()[0]),'loadavg':list(os.getloadavg()),'containers':[]}
result['memory']={k:v.strip() for k,v in (line.split(':',1) for line in open('/proc/meminfo')) if k in {'MemTotal','MemAvailable','SwapTotal','SwapFree'}}
for c in containers:
 env=dict(v.split('=',1) for v in c['Config'].get('Env',[]) if '=' in v)
 result['containers'].append({'service':c['Config']['Labels'].get('com.docker.compose.service'),'image':c['Image'],'revision':c['Config']['Labels'].get('org.opencontainers.image.revision'),'started_at':c['State']['StartedAt'],'oom_killed':c['State']['OOMKilled'],'restart_count':c['RestartCount'],'command':c['Config']['Cmd'],'flags':{k:env[k] for k in sorted(allowed&env.keys())},'compose_files':c['Config']['Labels'].get('com.docker.compose.project.config_files')})
result['stats']=[json.loads(v) for v in call(['docker','stats','--no-stream','--format','{{json .}}']).splitlines()]
print(json.dumps(result,sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['host', 'runtime', 'performance'])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--candidate-operations', type=Path,
                        help='Profile this reviewed source only inside the one-shot readonly observer; no deployment')
    args = parser.parse_args()
    container = 'iacp-controlplane-api-1' if args.mode == 'performance' else 'iacp-controlplane-terraform-worker-1'
    command = 'sudo -n python3 -' if args.mode == 'host' else 'sudo -n docker exec -i ' + container + ' python -'
    code = {'host': HOST, 'runtime': RUNTIME, 'performance': PERFORMANCE}[args.mode]
    if args.candidate_operations:
        if args.mode != 'performance':
            raise SystemExit('Candidate source is supported only in readonly performance mode')
        source = args.candidate_operations.read_text()
        # A separate module exists only inside docker exec's inspection process.
        # It cannot replace the running Uvicorn application's module or routes.
        prefix = ('import types,sys,hashlib\n'
                  "candidate=types.ModuleType('iacp.api.phase_c_candidate')\n"
                  "candidate.__package__='iacp.api'\n"
                  "sys.modules[candidate.__name__]=candidate\n"
                  f'exec(compile({source!r},"phase-c-reviewed-operations.py","exec"),candidate.__dict__)\n')
        code = prefix + code.replace('from iacp.api.operations import list_operations',
                                     'list_operations=candidate.list_operations')
        code = code.replace("principal=Principal(",
                            f"result['candidate_source_sha256']=hashlib.sha256({source.encode()!r}).hexdigest()\nprincipal=Principal(")
    completed = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                                'iacp-controlplane', command], input=code, text=True,
                               capture_output=True, timeout=90)
    if completed.returncode:
        raise SystemExit('Read-only snapshot failed; raw diagnostics suppressed')
    value = json.loads(completed.stdout)
    with args.output.open('x') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')
    print(json.dumps({'mode': args.mode, 'recorded_at': value['at'], 'output': str(args.output)}))


if __name__ == '__main__':
    main()
