"""Inicializadores fijos por distribución; sin comandos ni rutas del consumidor."""
import hashlib

NETWORK_MANAGER_FILE = '/etc/NetworkManager/system-connections/iacp-identity.nmconnection'

ALMA_PROGRAM = r'''
import hashlib,json,os,re,subprocess,sys,tempfile,uuid
from pathlib import Path
identifier,mac,program_sha=sys.argv[1:]
assert str(uuid.UUID(identifier))==identifier and re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',mac)
assert re.fullmatch(r'[0-9a-f]{64}',program_sha)
release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
assert release['ID'].strip('"')=='almalinux' and release['VERSION_ID'].strip('"').split('.')[0]=='__MAJOR__'
interfaces=[p for p in Path('/sys/class/net').glob('*/address') if p.read_text().strip().lower()==mac]
assert len(interfaces)==1
interface=interfaces[0].parent.name
assert re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',interface)
ifindex=int((interfaces[0].parent/'ifindex').read_text())
cloud_init=subprocess.run(['/usr/bin/cloud-init','status','--format','json'],capture_output=True,text=True,timeout=30)
assert cloud_init.returncode in (0,2), cloud_init.stderr
cloud_status=json.loads(cloud_init.stdout)
assert cloud_status.get('status')=='done'
assert Path('/var/lib/cloud/instance/boot-finished').is_file()
assert json.loads(Path('/var/lib/cloud/data/result.json').read_text()).get('v1',{}).get('errors')==[]
marker=Path('/var/lib/iacp/guest-identity.json')
assert not marker.exists(), 'An initializer never overwrites a previous identity'
network=Path('/etc/NetworkManager/system-connections/iacp-identity.nmconnection')
assert not network.exists()
def write(path,value):
    assert not path.is_symlink() and path.parent.is_dir()
    fd,temporary=tempfile.mkstemp(prefix='.iacp-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600);stream.write(value);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
content=('[connection]\nid=iacp-identity\nuuid='+str(uuid.uuid4())+'\ntype=ethernet\ninterface-name='+interface+
 '\nautoconnect=true\nautoconnect-priority=100\n[ethernet]\nmac-address='+mac+
 '\n[ipv4]\nmethod=auto\ndhcp-client-id=mac\nmay-fail=false\n[ipv6]\nmethod=disabled\n')
write(network,content)
machine=uuid.uuid4().hex+'\n'
machine_id=Path('/etc/machine-id')
assert not machine_id.is_symlink()
machine_id.write_text(machine);os.chmod(machine_id,0o444)
dbus=Path('/var/lib/dbus/machine-id')
if dbus.is_symlink():assert dbus.resolve()==Path('/etc/machine-id')
elif dbus.exists():dbus.write_text(machine);os.chmod(dbus,0o444)
# AlmaLinux images already receive DHCP through the connection materialized by
# Cloud-Init.  Re-loading/activating a second profile through NetworkManager's
# D-Bus from the confined QGA domain is both unnecessary and denied by SELinux;
# use read-only observation here and keep the identity profile as durable
# evidence for reconciliation.
dhcp=subprocess.run(['/usr/bin/nmcli','-g','DHCP4.OPTION','device','show',interface],check=True,
 capture_output=True,text=True,timeout=15).stdout
assert 'dhcp_server_identifier' in dhcp and 'dhcp_lease_time' in dhcp
result={'instance_id':identifier,'mac_address':mac,'mode':'almalinux-__MAJOR__-lab-identity-v1',
 'program_sha256':program_sha,'machine_id_sha256':hashlib.sha256(machine.encode()).hexdigest(),
 'network_sha256':hashlib.sha256(content.encode()).hexdigest(),'ifindex':ifindex,'network_manager_dhcp':True}
write(marker,json.dumps(result,sort_keys=True)+'\n')
'''

MODES = {'ubuntu-2604-lab-identity-v1', 'almalinux-9-lab-identity-v1', 'almalinux-10-lab-identity-v1'}


def image_program(mode, ubuntu_program):
    if mode == 'ubuntu-2604-lab-identity-v1':
        program = ubuntu_program.replace('VERSION_ID="24.04"', 'VERSION_ID="26.04"').replace(
            'ubuntu-2404-lab-identity-v1', mode)
    elif mode in {'almalinux-9-lab-identity-v1', 'almalinux-10-lab-identity-v1'}:
        program = ALMA_PROGRAM.replace('__MAJOR__', mode.split('-')[1])
    else:
        raise ValueError('GUEST_IMAGE_MODE_NOT_AUTHORIZED')
    return program, hashlib.sha256(program.encode()).hexdigest()
