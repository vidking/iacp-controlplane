"""Bootstrap root por SSH con host key fijada; datos sensibles solo por stdin."""
import base64
import json
import os
from pathlib import Path
import shlex

from .bootstrap_secrets import BootstrapSecretError

# Programa fijo sin entradas del cliente en argv. chpasswd recibe su entrada
# por pipe; las salidas de herramientas no se propagan a eventos ni excepciones.
PROGRAM = r'''
import json,os,subprocess,sys,tempfile
from pathlib import Path
p=json.load(sys.stdin)
assert os.getuid()==0
release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
expected={'ubuntu-2404':('ubuntu','24.04'),'ubuntu-2604':('ubuntu','26.04'),
 'almalinux-9':('almalinux','9'),'almalinux-10':('almalinux','10')}
os_id,version=expected[p['image_id']]
assert release['ID'].strip('"')==os_id
actual=release['VERSION_ID'].strip('"')
assert (actual==version if os_id=='ubuntu' else actual.split('.')[0]==version)
assert Path('/var/lib/cloud/instance/boot-finished').is_file()
assert json.loads(Path('/var/lib/cloud/data/result.json').read_text()).get('v1',{}).get('errors')==[]
marker=Path('/var/lib/iacp/commercial-bootstrap.json')
identity={'instance_id':p['instance_id'],'reference':p['reference']}
if marker.exists():
 assert json.loads(marker.read_text())==identity
 print('ALREADY_COMPLETED');sys.exit(0)
def write(path,content,mode):
 assert not path.is_symlink()
 path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 fd,tmp=tempfile.mkstemp(prefix='.iacp-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as stream:
   os.fchmod(stream.fileno(),mode);stream.write(content);stream.flush();os.fsync(stream.fileno())
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
# La contraseña root autoriza también SSH en el piloto LAB aprobado.
if p['ssh_public_keys']:
 write(Path('/root/.ssh/authorized_keys'),'\n'.join(p['ssh_public_keys'])+'\n',0o600)
write(Path('/etc/ssh/sshd_config.d/00-iacp-commercial-root.conf'),
 'PermitRootLogin yes\nPasswordAuthentication yes\nKbdInteractiveAuthentication no\n',0o600)
Path('/run/sshd').mkdir(mode=0o755,exist_ok=True)
subprocess.run(['/usr/sbin/sshd','-t'],check=True,capture_output=True,timeout=10)
effective=subprocess.run(['/usr/sbin/sshd','-T','-C','user=root,host=localhost,addr=127.0.0.1'],
 check=True,capture_output=True,text=True,timeout=10).stdout
assert 'permitrootlogin yes\n' in effective and 'passwordauthentication yes\n' in effective
subprocess.run(['/usr/sbin/chpasswd'],input=('root:'+p['password']+'\n').encode(),
 check=True,capture_output=True,timeout=15)
if os_id=='almalinux':
 for path in ['/root/.ssh','/etc/ssh/sshd_config.d/00-iacp-commercial-root.conf']:
  if Path(path).exists():subprocess.run(['/usr/sbin/restorecon','-RF',path],check=True,capture_output=True,timeout=15)
subprocess.run(['/usr/bin/systemctl','reload-or-restart','ssh' if os_id=='ubuntu' else 'sshd'],check=True,capture_output=True,timeout=20)
write(marker,json.dumps(identity,sort_keys=True)+'\n',0o600)
print('COMPLETED')
'''
COMMAND='sudo -n /usr/bin/python3 -c '+shlex.quote(PROGRAM)


class SecureSSHBootstrap:
    def observe(self,instance,target,reference):
        from urllib.parse import urlencode
        from .guest_identity import GuestClient
        try:
            client=GuestClient.configured().client
            client.observe(target,preflight=True)
            result=client._request('GET',target.path+'/agent/file-read?'+urlencode({
                'file':'/var/lib/iacp/commercial-bootstrap.json','count':4096}))
            marker=json.loads(result['content'])
            if result.get('truncated') or marker!={'instance_id':instance.id,'reference':reference}:
                raise ValueError()
        except Exception:
            raise BootstrapSecretError('BOOTSTRAP_RECONCILIATION_REQUIRED') from None
        return {'status':'completed'}

    def apply(self,instance,target,reference,password,public_keys,username):
        import paramiko
        from urllib.parse import urlencode
        from .guest_identity import GuestClient
        from ipaddress import IPv4Address
        if username not in {'ubuntu','root'} or not instance.ip_addresses:
            raise BootstrapSecretError('BOOTSTRAP_GUEST_NOT_READY')
        try:
            keyfile=Path(os.environ['IACP_GUEST_BOOTSTRAP_SSH_KEY_FILE'])
            if keyfile.stat().st_mode & 0o027:
                raise ValueError()
            # Clave operativa montada; no se copia, serializa ni incorpora a DB.
            key=paramiko.PKey.from_path(keyfile)
            client=GuestClient.configured().client
            client.observe(target,preflight=True)
            def read(path):
                result=client._request('GET',target.path+'/agent/file-read?'+urlencode({'file':path,'count':8192}))
                if result.get('truncated'):raise ValueError()
                return result['content']
            read('/var/lib/cloud/instance/boot-finished')
            if json.loads(read('/var/lib/cloud/data/result.json')).get('v1',{}).get('errors')!=[]:
                raise ValueError()
            host=read('/etc/ssh/ssh_host_ed25519_key.pub').split()
            if host[0]!='ssh-ed25519':raise ValueError()
            hostkey=paramiko.Ed25519Key(data=base64.b64decode(host[1],validate=True))
            address=str(IPv4Address(instance.ip_addresses[0]))
            with paramiko.SSHClient() as ssh:
                ssh.get_host_keys().add(address,'ssh-ed25519',hostkey)
                ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
                ssh.connect(address,username=username,pkey=key,timeout=10,auth_timeout=10,
                    banner_timeout=10,allow_agent=False,look_for_keys=False)
                stdin,stdout,stderr=ssh.exec_command(COMMAND if username!='root' else '/usr/bin/python3 -c '+shlex.quote(PROGRAM),timeout=60)
                stdin.write(json.dumps({'instance_id':instance.id,'reference':reference,
                    'password':password,'ssh_public_keys':public_keys,'image_id':getattr(instance,'image','ubuntu-2404')}))
                stdin.flush();stdin.channel.shutdown_write()
                output=stdout.read(128)
                if stdout.channel.recv_exit_status()!=0 or output.strip() not in {b'COMPLETED',b'ALREADY_COMPLETED'}:
                    raise ValueError()
        except Exception:
            raise BootstrapSecretError('BOOTSTRAP_SSH_DELIVERY_UNCONFIRMED') from None
        return {'status':'completed','console_password_configured':True,'root_ssh_password_enabled':True}
