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
import json,os,re,subprocess,sys,tempfile
from pathlib import Path
identifier,mac,mode,expected_ip,expected_gateway=sys.argv[1:]
assert mode in {'dhcp','static'}
if mode=='dhcp':
 marker=json.loads(Path('/var/lib/iacp/guest-identity.json').read_text())
 assert marker['instance_id']==identifier and marker['mac_address']==mac
else:
 cloud=subprocess.run(['/usr/bin/cloud-init','status','--wait','--long'],capture_output=True,text=True,timeout=90)
 assert cloud.returncode in (0,2) and re.search(r'(?m)^status:\s*done\s*$',cloud.stdout)
 assert re.search(r'(?m)^errors:\s*\[\]\s*$',cloud.stdout)
 if cloud.returncode==2:
  lines=[line.strip() for line in cloud.stdout.split('recoverable_errors:',1)[1].splitlines() if line.strip()]
  assert len(lines)>1 and lines[0]=='DEPRECATED:' and all(line.startswith('- ') and 'deprecated' in line.lower() for line in lines[1:])
if mode=='static':
 import shutil
 ip=shutil.which('ip');assert ip
 interfaces=json.loads(subprocess.run([ip,'-j','address'],check=True,capture_output=True,text=True).stdout)
 matches=[entry for entry in interfaces if entry.get('address','').lower()==mac]
 assert len(matches)==1
 interface=matches[0]
 expected_address,expected_prefix=expected_ip.split('/')
 assert any(entry.get('local')==expected_address and entry.get('prefixlen')==int(expected_prefix) for entry in interface.get('addr_info',[]))
 routes=json.loads(subprocess.run([ip,'-j','route','show','default'],check=True,capture_output=True,text=True).stdout)
 assert any(route.get('gateway')==expected_gateway and route.get('dev')==interface['ifname'] for route in routes)
 resolver=shutil.which('resolvectl')
 if resolver:
  dns=subprocess.run([resolver,'dns',interface['ifname']],check=True,capture_output=True,text=True).stdout
 else:
  nmcli=shutil.which('nmcli');assert nmcli
  dns=subprocess.run([nmcli,'-g','IP4.DNS','device','show',interface['ifname']],check=True,capture_output=True,text=True).stdout
 assert all(value in dns for value in ['1.1.1.1','8.8.8.8'])
 marker=Path('/var/lib/iacp/static-identity.json')
 value={'instance_id':identifier,'mac_address':mac,'ipv4_cidr':expected_ip,'gateway':expected_gateway}
 assert not marker.is_symlink()
 if marker.exists():assert json.loads(marker.read_text())==value
 else:
  marker.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  descriptor=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(descriptor,'w') as output:json.dump(value,output,sort_keys=True)
 key=Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
 assert len(key)>=2 and key[0]=='ssh-ed25519'
 print(json.dumps({'instance_id':identifier,'mac_address':mac,'host_key':' '.join(key[:2]),'dns_servers':['1.1.1.1','8.8.8.8']}))
 sys.exit(0)
import yaml
network=yaml.safe_load(Path('/etc/netplan/50-cloud-init.yaml').read_text())['network']
matching=[name for name,conf in network['ethernets'].items() if conf.get('match',{}).get('macaddress','').lower()==mac]
assert len(matching)==1
interface=matching[0]
settings={'nameservers':{'addresses':['1.1.1.1','8.8.8.8']}}
if mode=='dhcp':
 settings['dhcp4-overrides']={'use-dns':False}
else:
 conf=network['ethernets'][interface]
 assert expected_ip in conf.get('addresses',[]) and not conf.get('dhcp4')
 gateways=[r.get('via') for r in conf.get('routes',[]) if r.get('to') in ('default','0.0.0.0/0')]
 assert expected_gateway==conf.get('gateway4') or expected_gateway in gateways
 marker=Path('/var/lib/iacp/static-identity.json')
 value={'instance_id':identifier,'mac_address':mac,'ipv4_cidr':expected_ip,'gateway':expected_gateway}
 if marker.exists():assert json.loads(marker.read_text())==value
 else:
  marker.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
  descriptor=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
  with os.fdopen(descriptor,'w') as output:json.dump(value,output,sort_keys=True)
desired=json.dumps({'network':{'version':2,'ethernets':{interface:settings}}},sort_keys=True)+'\n'
path=Path('/etc/netplan/98-iacp-service-dns.yaml')
assert not path.is_symlink()
if not path.exists() or path.read_text()!=desired:
 fd,temporary=tempfile.mkstemp(prefix='.iacp-dns-',dir=path.parent)
 try:
  with os.fdopen(fd,'w') as output:
   os.fchmod(output.fileno(),0o600);output.write(desired);output.flush();os.fsync(output.fileno())
  os.replace(temporary,path)
 finally:
  if os.path.exists(temporary):os.unlink(temporary)
 subprocess.run(['/usr/sbin/netplan','apply'],check=True,capture_output=True,timeout=45)
if mode=='static':
 routes=json.loads(subprocess.run(['/usr/sbin/ip','-j','route','show','default'],check=True,capture_output=True,text=True).stdout)
 assert any(route.get('gateway')==expected_gateway for route in routes)
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
    if not service and not static:
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
