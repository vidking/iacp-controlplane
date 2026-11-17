"""Preparación de servicios por canal Proxmox autenticado; no por TOFU SSH."""

import json
import re
import time
from ipaddress import ip_address
from api.db import database
from api.protection import is_protected_vmid
from api.service import now
from worker.guest_identity import (
    GuestIdentityError,
    _mac_from_config,
    _wait_for_pool_membership,
)
from worker.golden import configured_pool_id

# Solo datos públicos salen del invitado. La identidad la acredita el paso previo.
PROGRAM = r"""
import json,os,re,shutil,socket,subprocess,sys,tempfile
from pathlib import Path
identifier,mac,mode,expected_ip,expected_gateway=sys.argv[1:]
assert mode in {'dhcp','static'}
def run(argv,timeout=60):
 return subprocess.run(argv,check=True,capture_output=True,text=True,timeout=timeout).stdout
cloud=subprocess.run(['/usr/bin/cloud-init','status','--wait','--long'],capture_output=True,text=True,timeout=120)
assert cloud.returncode in (0,2) and re.search(r'(?m)^status:\s*done\s*$',cloud.stdout)
assert re.search(r'(?m)^errors:\s*\[\]\s*$',cloud.stdout)
if cloud.returncode==2:
 lines=[line.strip() for line in cloud.stdout.split('recoverable_errors:',1)[1].splitlines() if line.strip()]
 assert len(lines)>1 and lines[0]=='DEPRECATED:' and all(line.startswith('- ') and 'deprecated' in line.lower() for line in lines[1:])
ip=shutil.which('ip');assert ip
interfaces=json.loads(run([ip,'-j','address']))
matches=[entry for entry in interfaces if entry.get('address','').lower()==mac]
assert len(matches)==1
interface=matches[0];device=interface['ifname']
marker=Path('/var/lib/iacp/'+('static-identity.json' if mode=='static' else 'service-dhcp-identity.json'))
value={'instance_id':identifier,'mac_address':mac,'mode':mode}
if mode=='static':value.update(ipv4_cidr=expected_ip,gateway=expected_gateway)
assert not marker.is_symlink()
if marker.exists():
 previous=json.loads(marker.read_text())
 assert all(previous.get(key)==val for key,val in value.items() if key!='mode')
if mode=='dhcp':
 nmcli=shutil.which('nmcli')
 if nmcli and not Path('/etc/netplan/50-cloud-init.yaml').exists():
  uuid=run([nmcli,'-g','GENERAL.CON-UUID','device','show',device]).strip();assert uuid
  configured=run([nmcli,'-g','ipv4.dhcp-client-id,ipv4.ignore-auto-dns,ipv4.dns','connection','show','uuid',uuid])
  if 'mac' not in configured or '1.1.1.1' not in configured or '8.8.8.8' not in configured or 'yes' not in configured:
   run([nmcli,'connection','modify','uuid',uuid,'ipv4.dhcp-client-id','mac','ipv4.ignore-auto-dns','yes','ipv4.dns','1.1.1.1 8.8.8.8'])
   run([nmcli,'connection','up','uuid',uuid,'ifname',device],timeout=90)
 else:
  import yaml
  network=yaml.safe_load(Path('/etc/netplan/50-cloud-init.yaml').read_text())['network']
  names=[name for name,conf in network['ethernets'].items() if conf.get('match',{}).get('macaddress','').lower()==mac]
  assert len(names)==1
  settings={'dhcp4':True,'dhcp-identifier':'mac','dhcp4-overrides':{'use-dns':False},'nameservers':{'addresses':['1.1.1.1','8.8.8.8']}}
  desired=json.dumps({'network':{'version':2,'ethernets':{names[0]:settings}}},sort_keys=True)+'\n'
  path=Path('/etc/netplan/98-iacp-service-dns.yaml');assert not path.is_symlink()
  if not path.exists() or path.read_text()!=desired:
   fd,temporary=tempfile.mkstemp(prefix='.iacp-dns-',dir=path.parent)
   with os.fdopen(fd,'w') as output:
    os.fchmod(output.fileno(),0o600);output.write(desired)
   os.replace(temporary,path);run(['/usr/sbin/netplan','apply'])
else:
 expected_address,expected_prefix=expected_ip.split('/')
 assert any(entry.get('local')==expected_address and entry.get('prefixlen')==int(expected_prefix) for entry in interface.get('addr_info',[]))
 routes=json.loads(run([ip,'-j','route','show','default']))
 assert any(route.get('gateway')==expected_gateway and route.get('dev')==device for route in routes)
 try:
  with socket.create_connection(('1.1.1.1',443),timeout=5):pass
 except OSError:pass
 neighbors=json.loads(run([ip,'-j','neigh','show','to',expected_gateway]))
 def gateway_ready(records,gateway,device):
  return any(record.get('dst')==gateway and record.get('dev')==device and record.get('lladdr') and set(record.get('state',[])).intersection({'REACHABLE','STALE','DELAY','PROBE','PERMANENT'}) for record in records)
 assert gateway_ready(neighbors,expected_gateway,device), 'IACP_GATEWAY_UNREACHABLE'
resolver=shutil.which('resolvectl')
if resolver:dns=run([resolver,'dns',device])
else:
 nmcli=shutil.which('nmcli');assert nmcli
 dns=run([nmcli,'-g','IP4.DNS','device','show',device])
assert all(value in dns for value in ['1.1.1.1','8.8.8.8'])
if not marker.exists():
 marker.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 descriptor=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(descriptor,'w') as output:json.dump(value,output,sort_keys=True)
key=Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
assert len(key)>=2 and key[0]=='ssh-ed25519'
print(json.dumps({'instance_id':identifier,'mac_address':mac,'host_key':' '.join(key[:2]),'dns_servers':['1.1.1.1','8.8.8.8']}))

"""


def prepare(client, *, node, vmid, name, instance_id, heartbeat, spec=None):
    with database() as db:
        service = db.execute(
            "SELECT service_id, configuration_json, status FROM service_instances WHERE instance_id=?",
            (instance_id,),
        ).fetchone()
        instance = db.execute(
            "SELECT spec_json FROM instances WHERE instance_id=?", (instance_id,)
        ).fetchone()
    spec = spec or (json.loads(instance["spec_json"]) if instance else {})
    static = spec.get("network_profile") == "prod_static"
    if service and service["status"] in {"delivered", "deleted", "cancelled"}:
        return None
    if not service and not static and not spec.get("prepare_identity"):
        return None
    if is_protected_vmid(vmid):
        raise GuestIdentityError("SERVICE_BOOTSTRAP_PROTECTED")
    config = client.vm_config(node, vmid)
    if static:
        network = config.get("net0", "")
        settings = dict(
            piece.split("=", 1) for piece in network.split(",") if "=" in piece
        )
        ipsettings = dict(
            piece.split("=", 1)
            for piece in config.get("ipconfig0", "").split(",")
            if "=" in piece
        )
        mac = settings.get("virtio") or settings.get("e1000") or ""
        if (
            config.get("name") != name
            or config.get("template")
            or settings.get("bridge") != "vmbr0"
            or settings.get("tag") != "211"
            or ipsettings.get("ip") != spec.get("ipv4_cidr")
            or ipsettings.get("gw") != "190.0.231.1"
            or spec.get("ipv4_gateway") != "190.0.231.1"
            or not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac)
        ):
            raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_IDENTITY_MISMATCH")
        from ipaddress import ip_interface, ip_network

        address = ip_interface(spec["ipv4_cidr"])
        if (
            address.ip not in ip_network("190.0.231.0/24")
            or address.network.prefixlen != 24
        ):
            raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_PREFIX_MISMATCH")
        mac = mac.lower()
    else:
        mac = _mac_from_config(config, name)
        if "tag=" in config.get("net0", ""):
            raise GuestIdentityError("DHCP_UNEXPECTED_VLAN")
    _wait_for_pool_membership(
        client,
        node=node,
        vmid=vmid,
        name=name,
        pool_id=configured_pool_id(),
        heartbeat=heartbeat,
    )
    result = client.guest_agent_exec(
        node,
        vmid,
        [
            "/usr/bin/python3",
            "-c",
            PROGRAM,
            instance_id,
            mac,
            "static" if static else "dhcp",
            spec.get("ipv4_cidr") or "",
            spec.get("ipv4_gateway") or "",
        ],
    )
    pid = result.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise GuestIdentityError("SERVICE_BOOTSTRAP_PID_INVALID")
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        heartbeat()
        status = client.guest_agent_exec_status(node, vmid, pid)
        if status.get("exited"):
            if status.get("exitcode") != 0:
                if "IACP_GATEWAY_UNREACHABLE" in str(status.get("err-data", "")):
                    raise GuestIdentityError("STATIC_GATEWAY_UNREACHABLE")
                raise GuestIdentityError("SERVICE_BOOTSTRAP_GUEST_FAILED")
            observed = json.loads(status.get("out-data", "{}"))
            break
        time.sleep(1)
    else:
        raise GuestIdentityError("SERVICE_BOOTSTRAP_TIMEOUT")
    if (
        observed.get("instance_id") != instance_id
        or observed.get("mac_address") != mac
        or observed.get("dns_servers") != ["1.1.1.1", "8.8.8.8"]
        or not re.fullmatch(
            r"ssh-ed25519 [A-Za-z0-9+/=]+", observed.get("host_key", "")
        )
    ):
        raise GuestIdentityError("SERVICE_BOOTSTRAP_IDENTITY_MISMATCH")
    addresses = []
    for interface in client.guest_agent_interfaces(node, vmid):
        if str(interface.get("hardware-address", "")).lower() != mac:
            continue
        for entry in interface.get("ip-addresses", []):
            try:
                address = ip_address(entry["ip-address"])
            except (KeyError, ValueError):
                continue
            if address.version == 4 and not (
                address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
            ):
                addresses.append(str(address))
    if static and (
        len(set(addresses)) != 1 or addresses[0] != spec["ipv4_cidr"].split("/")[0]
    ):
        raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_ADDRESS_MISMATCH")
    if len(set(addresses)) != 1:
        raise GuestIdentityError("SERVICE_BOOTSTRAP_ADDRESS_AMBIGUOUS")
    binding = {
        "instance_id": instance_id,
        "vmid": vmid,
        "node": node,
        "address": addresses[0],
        "host_key": observed["host_key"],
        "domain": json.loads(service["configuration_json"]).get("domain")
        if service
        else None,
        "dns_servers": observed["dns_servers"],
        "source": "qemu-guest-agent",
    }
    if not service:
        return binding
    with database() as db:
        db.execute(
            """INSERT INTO service_bindings VALUES (?, ?, ?) ON CONFLICT(service_id)
            DO UPDATE SET binding_json=excluded.binding_json, observed_at=excluded.observed_at""",
            (service["service_id"], json.dumps(binding), now()),
        )
    return binding
