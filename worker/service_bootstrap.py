"""Preparación de servicios por canal Proxmox autenticado; no por TOFU SSH."""

from dataclasses import dataclass, field
import json
import re
import time
from ipaddress import ip_address
from cryptography.fernet import InvalidToken
from api.db import database
from api.protection import is_protected_vmid
from api.service import now
from api.service_secrets import decrypt, encrypt
from worker.guest_identity import (
    GuestIdentityError,
    _mac_from_config,
    _wait_for_pool_membership,
)
from worker.golden import configured_pool_id
from worker.proxmox import ProxmoxClient, ProxmoxError


def installer_key_pattern(public_key: str) -> str:
    """Restringe la coincidencia al tipo y al cuerpo de la clave de instalación."""
    match = re.fullmatch(r"ssh-ed25519 ([A-Za-z0-9+/=]+)(?:[ \t]+[^\r\n]*)?", public_key)
    if not match:
        raise ValueError("SERVICE_INSTALLER_KEY_INVALID")
    return rf"(?:^|[ \t])ssh-ed25519[ \t]+{re.escape(match[1])}(?:[ \t]|$)"


@dataclass(frozen=True)
class HandoffEvidence:
    key_absent: bool
    snapshot: dict = field(repr=False)


def _handoff_snapshot(db, job_id):
    row = db.execute(
        """SELECT j.generation, j.status AS job_status, s.service_id, s.secrets_encrypted,
        i.instance_id, i.operation_id, i.name, i.vmid, i.node, i.spec_json, i.updated_at,
        b.binding_json, b.observed_at
        FROM service_jobs j JOIN service_instances s ON s.service_id=j.service_id
        JOIN instances i ON i.instance_id=s.instance_id
        JOIN service_bindings b ON b.service_id=s.service_id
        JOIN vmid_leases l ON l.vmid=i.vmid AND l.operation_id=i.operation_id
        WHERE j.job_id=? AND j.action='handoff' AND j.status IN ('failed','uncertain')
        AND s.status=j.status AND s.secrets_encrypted IS NOT NULL
        AND i.environment='lab' AND i.status='ready' AND i.power_state='running'
        AND l.status='held'
        AND NOT EXISTS (
            SELECT 1 FROM operations o WHERE o.instance_id=i.instance_id
            AND o.status IN ('pending','running','planning','applying','destroying','powering','uncertain')
        )""",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def _handoff_file(client, node, vmid, path):
    result = client.guest_agent_file_read(node, vmid, path)
    if not isinstance(result, dict) or result.get("truncated"):
        raise ValueError("SERVICE_HANDOFF_FILE_UNVERIFIED")
    content = result.get("content")
    if not isinstance(content, str) or len(content.encode()) >= 8192 or "\x00" in content:
        raise ValueError("SERVICE_HANDOFF_FILE_UNVERIFIED")
    return content


def observe_handoff(job_id: str, *, client=None) -> HandoffEvidence | None:
    """Lee solo la identidad del invitado y el archivo de claves administrado."""
    with database() as db:
        snapshot = _handoff_snapshot(db, job_id)
    if snapshot is None or is_protected_vmid(snapshot["vmid"]):
        return None
    try:
        binding = json.loads(snapshot["binding_json"])
        if not isinstance(binding, dict) or (
            binding.get("source") != "qemu-guest-agent"
            or binding.get("instance_id") != snapshot["instance_id"]
            or binding.get("vmid") != snapshot["vmid"]
            or binding.get("node") != snapshot["node"]
        ):
            return None
        node, vmid = snapshot["node"], snapshot["vmid"]
        if not isinstance(node, str) or not node:
            return None
        expected_host_key = binding.get("host_key", "")
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/=]+", expected_host_key):
            return None

        secrets = decrypt(snapshot["secrets_encrypted"])
        key_pattern = installer_key_pattern(secrets["ssh_public_key"])
        client = client or ProxmoxClient()
        config = client.vm_config(node, vmid)
        if config.get("template"):
            return None
        mac = _mac_from_config(config, snapshot["name"])
        spec = json.loads(snapshot["spec_json"])
        mode = "static" if spec.get("network_profile") == "prod_static" else "dhcp"
        identity_path = f"/var/lib/iacp/{'static' if mode == 'static' else 'service-dhcp'}-identity.json"
        marker = json.loads(_handoff_file(client, node, vmid, identity_path))
        if not isinstance(marker, dict) or (
            marker.get("instance_id") != snapshot["instance_id"]
            or marker.get("mac_address") != mac
            or marker.get("mode") != mode
        ):
            return None
        host_key = _handoff_file(client, node, vmid, "/etc/ssh/ssh_host_ed25519_key.pub")
        if " ".join(host_key.split()[:2]) != expected_host_key:
            return None
        authorized = _handoff_file(client, node, vmid, "/root/.ssh/authorized_keys")
        key_absent = not any(re.search(key_pattern, line) for line in authorized.splitlines())
        return HandoffEvidence(key_absent=key_absent, snapshot=snapshot)
    except (InvalidToken, KeyError, OSError, ProxmoxError, RuntimeError, TypeError, ValueError):
        return None


def confirm_handoff_evidence(db, job_id: str, evidence: HandoffEvidence | None) -> bool:
    """Revalida los datos de identidad dentro de la transacción del reintento."""
    return evidence is not None and _handoff_snapshot(db, job_id) == evidence.snapshot


def finish_handoff_from_observation(db, job_id: str, evidence: HandoffEvidence | None) -> bool:
    if evidence is None or not evidence.key_absent or not confirm_handoff_evidence(db, job_id, evidence):
        return False
    service_id = evidence.snapshot["service_id"]
    completed_at = now()
    db.execute(
        """UPDATE service_jobs SET status='succeeded', phase='completed', error='', finished_at=?
        WHERE job_id=?""",
        (completed_at, job_id),
    )
    db.execute(
        """UPDATE service_instances SET status='delivered', secrets_encrypted=NULL, updated_at=?
        WHERE service_id=?""",
        (completed_at, service_id),
    )
    db.execute("DELETE FROM service_credential_retrievals WHERE service_id=?", (service_id,))
    db.execute(
        """INSERT INTO service_events(job_id, phase, status, occurred_at)
        VALUES (?, 'recovered_handoff_qga', 'succeeded', ?)""",
        (job_id, completed_at),
    )
    return True

# Solo datos públicos salen del invitado. La identidad la acredita el paso previo.
PROGRAM = r"""
import json,os,re,shutil,socket,subprocess,sys,tempfile,time
from pathlib import Path
identifier,mac,mode,expected_ip,expected_gateway=sys.argv[1:]
assert mode in {'dhcp','static'}
def run(argv,timeout=60):
 return subprocess.run(argv,check=True,capture_output=True,text=True,timeout=timeout).stdout
def cloud_ready(result):
 assert result.returncode in (0,2)
 status=json.loads(result.stdout)
 assert status.get('status')=='done' and status.get('errors')==[]
 recoverable=status.get('recoverable_errors') or {}
 assert isinstance(recoverable,dict) and set(recoverable)<= {'DEPRECATED'}
 assert all(isinstance(messages,list) and all(isinstance(message,str) and 'deprecated' in message.lower() for message in messages) for messages in recoverable.values())
cloud=subprocess.run(['/usr/bin/cloud-init','status','--wait','--long','--format','json'],capture_output=True,text=True,timeout=120)
cloud_ready(cloud)
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
   run([shutil.which('systemctl'),'restart','systemd-networkd'])
  def mac_lease(text,mac):
   lease=dict(line.split('=',1) for line in text.splitlines() if '=' in line)
   client=lease.get('CLIENTID','').lower().replace(':','').replace('-','')
   return lease.get('ADDRESS','').split('/')[0] if client=='01'+mac.replace(':','') else None
  lease_file=Path('/run/systemd/netif/leases')/str(interface['ifindex'])
  deadline=time.monotonic()+90
  while True:
   lease_ip=mac_lease(lease_file.read_text(),mac) if lease_file.exists() else None
   if lease_ip:break
   assert time.monotonic()<deadline, 'IACP_DHCP_MAC_LEASE_UNAVAILABLE'
   time.sleep(1)
else:
 expected_address,expected_prefix=expected_ip.split('/')
 assert any(entry.get('local')==expected_address and entry.get('prefixlen')==int(expected_prefix) for entry in interface.get('addr_info',[]))
 routes=json.loads(run([ip,'-j','route','show','default']))
 assert any(route.get('gateway')==expected_gateway and route.get('dev')==device for route in routes)
 try:
  with socket.create_connection(('1.1.1.1',443),timeout=5):pass
 except OSError:pass
 def gateway_ready(records,gateway,device):
  return any(record.get('dst')==gateway and record.get('dev')==device and record.get('lladdr') and set(record.get('state',[])).intersection({'REACHABLE','STALE','DELAY','PROBE','PERMANENT'}) for record in records)
 neighbors=json.loads(run([ip,'-j','neigh','show','to',expected_gateway]))
 assert gateway_ready(neighbors,expected_gateway,device), 'IACP_GATEWAY_UNREACHABLE'
def has_expected_dns(output):
 observed=set(re.findall(r'(?<![0-9.])[0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}[.][0-9]{1,3}(?![0-9.])',output))
 return {'1.1.1.1','8.8.8.8'}<=observed
def observed_dns(device):
 resolver=shutil.which('resolvectl')
 if resolver:
  resolved=subprocess.run([resolver,'dns',device],capture_output=True,text=True,timeout=30)
  # systemd-resolved may be installed while inactive. In that case the command
  # can succeed without reporting per-link DNS; use the active network manager.
  if resolved.returncode==0 and has_expected_dns(resolved.stdout):return resolved.stdout
 nmcli=shutil.which('nmcli');nm_output=''
 if nmcli:
  observed=subprocess.run([nmcli,'-g','IP4.DNS','device','show',device],capture_output=True,text=True,timeout=30)
  nm_output=observed.stdout
  if observed.returncode==0 and has_expected_dns(nm_output):return nm_output
 resolver_file=Path('/etc/resolv.conf')
 if resolver_file.is_file():
  configured=resolver_file.read_text()
  if has_expected_dns(configured):return configured
 assert nmcli
 return nm_output
dns=observed_dns(device)
assert has_expected_dns(dns)
if not marker.exists():
 marker.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
 descriptor=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(descriptor,'w') as output:json.dump(value,output,sort_keys=True)
key=Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
assert len(key)>=2 and key[0]=='ssh-ed25519'
print(json.dumps({'instance_id':identifier,'mac_address':mac,'host_key':' '.join(key[:2]),'dns_servers':['1.1.1.1','8.8.8.8'],'ipv4':locals().get('lease_ip')}))

"""


def prepare(client, *, node, vmid, name, instance_id, heartbeat, spec=None):
    with database() as db:
        service = db.execute(
            "SELECT service_id, configuration_json, secrets_encrypted, status FROM service_instances WHERE instance_id=?",
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
        from api.catalog import prod_static_proxmox_network

        expected_network = prod_static_proxmox_network()
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
            or settings.get("bridge") != expected_network["bridge"]
            or "tag" in settings
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
            raise GuestIdentityError("DHCP_UNEXPECTED_NIC_TAG")
    _wait_for_pool_membership(
        client,
        node=node,
        vmid=vmid,
        name=name,
        pool_id=configured_pool_id(),
        heartbeat=heartbeat,
    )
    if static and spec.get("image") == "fedora-45":
        return _prepare_fedora_static_network(
            client,
            node=node,
            vmid=vmid,
            name=name,
            instance_id=instance_id,
            mac=mac,
            expected_ip=spec["ipv4_cidr"].split("/", 1)[0],
            heartbeat=heartbeat,
        )
    # Solo se repiten lecturas: un POST exec incierto nunca se vuelve a enviar.
    deadline = time.monotonic() + 180
    while True:
        heartbeat()
        try:
            client.guest_agent_interfaces(node, vmid)
            break
        except ProxmoxError:
            pass
        if time.monotonic() >= deadline:
            raise GuestIdentityError("GUEST_IDENTITY_AGENT_UNAVAILABLE")
        time.sleep(2)

    def guest_command(argv):
        result = client.guest_agent_exec(node, vmid, argv)
        pid = result.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise GuestIdentityError("SERVICE_BOOTSTRAP_PID_INVALID")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            heartbeat()
            status = client.guest_agent_exec_status(node, vmid, pid)
            if status.get("exited"):
                return status
            time.sleep(1)
        raise GuestIdentityError("SERVICE_BOOTSTRAP_TIMEOUT")

    prepared_image = spec.get("image") in {"almalinux-9", "almalinux-10"}
    arguments = [
        instance_id,
        mac,
        "static" if static else "dhcp",
        spec.get("ipv4_cidr") or "",
        spec.get("ipv4_gateway") or "",
    ]
    if prepared_image:
        helper = "/usr/local/libexec/iacp-guest-bootstrap"
        probe = guest_command(["/usr/bin/test", "-x", helper])
        if probe.get("exitcode") != 0:
            raise GuestIdentityError("IMAGE_BOOTSTRAP_REQUIRED")
        status = guest_command([helper] + arguments)
    else:
        status = guest_command(["/usr/bin/python3", "-c", PROGRAM] + arguments)
    if status.get("exitcode") != 0:
        if "IACP_GATEWAY_UNREACHABLE" in str(status.get("err-data", "")):
            raise GuestIdentityError("STATIC_GATEWAY_UNREACHABLE")
        raise GuestIdentityError("SERVICE_BOOTSTRAP_GUEST_FAILED")
    observed = json.loads(status.get("out-data", "{}"))
    if prepared_image and observed.get("bootstrap_protocol") != 1:
        raise GuestIdentityError("IMAGE_BOOTSTRAP_PROTOCOL_MISMATCH")
    if (
        observed.get("instance_id") != instance_id
        or observed.get("mac_address") != mac
        or observed.get("dns_servers") != ["1.1.1.1", "8.8.8.8"]
        or not re.fullmatch(
            r"ssh-ed25519 [A-Za-z0-9+/=]+", observed.get("host_key", "")
        )
    ):
        raise GuestIdentityError("SERVICE_BOOTSTRAP_IDENTITY_MISMATCH")
    deadline = time.monotonic() + 120
    while True:
        heartbeat()
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
        lease_ip = observed.get("ipv4")
        if lease_ip and lease_ip in addresses:
            addresses = [lease_ip]
        if len(set(addresses)) == 1 and (not lease_ip or lease_ip in addresses):
            if static and addresses[0] != spec["ipv4_cidr"].split("/")[0]:
                raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_ADDRESS_MISMATCH")
            break
        if time.monotonic() >= deadline:
            raise GuestIdentityError("SERVICE_BOOTSTRAP_ADDRESS_AMBIGUOUS")
        time.sleep(2)
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
    if not service["secrets_encrypted"]:
        raise GuestIdentityError("SERVICE_MAINTENANCE_CREDENTIALS_UNAVAILABLE")
    credentials = decrypt(service["secrets_encrypted"])
    maintenance_access = {
        "private_key": credentials.get("management_ssh_private_key"),
        "public_key": credentials.get("management_ssh_public_key"),
        "installer_public_key": credentials.get("ssh_public_key"),
    }
    if not all(maintenance_access.values()):
        raise GuestIdentityError("SERVICE_MAINTENANCE_CREDENTIALS_UNAVAILABLE")
    stored_binding = dict(binding)
    stored_binding["maintenance_access_encrypted"] = encrypt(maintenance_access)
    with database() as db:
        db.execute(
            """INSERT INTO service_bindings VALUES (?, ?, ?) ON CONFLICT(service_id)
            DO UPDATE SET binding_json=excluded.binding_json, observed_at=excluded.observed_at""",
            (service["service_id"], json.dumps(stored_binding), now()),
        )
    return binding


def _prepare_fedora_static_network(
    client,
    *,
    node: str,
    vmid: int,
    name: str,
    instance_id: str,
    mac: str,
    expected_ip: str,
    heartbeat,
):
    """Verify Fedora's static address from read-only Guest Agent metadata.

    Fedora confines QEMU Guest Agent under SELinux. Its network inventory is
    available, but running cloud-init/ip or reading SSH host keys from that
    context is denied. Avoid those commands and bind the expected address to
    the configured NIC MAC instead.
    """

    from ipaddress import ip_address

    expected_address = ip_address(expected_ip)
    if expected_address.version != 4 or expected_address.is_unspecified:
        raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_ADDRESS_INVALID")
    deadline = time.monotonic() + 180
    while True:
        heartbeat()
        try:
            interfaces = client.guest_agent_interfaces(node, vmid)
        except ProxmoxError as error:
            if error.status in {401, 403}:
                raise GuestIdentityError(
                    "SERVICE_BOOTSTRAP_GUEST_AGENT_PERMISSION_REQUIRED"
                ) from error
            interfaces = []
        matches = [
            interface
            for interface in interfaces
            if str(interface.get("hardware-address", "")).lower() == mac
        ]
        if len(matches) > 1:
            raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_INTERFACE_AMBIGUOUS")
        if matches:
            addresses = []
            for entry in matches[0].get("ip-addresses") or []:
                try:
                    address = ip_address(str(entry.get("ip-address", "")))
                except ValueError:
                    continue
                if address.version == 4 and not (
                    address.is_loopback
                    or address.is_link_local
                    or address.is_multicast
                    or address.is_unspecified
                ):
                    addresses.append(address)
            if expected_address in addresses and all(
                address == expected_address for address in addresses
            ):
                return {
                    "instance_id": instance_id,
                    "vmid": vmid,
                    "node": node,
                    "address": str(expected_address),
                    "host_key": None,
                    "domain": None,
                    "dns_servers": ["1.1.1.1", "8.8.8.8"],
                    "source": "qemu-guest-agent-network",
                    "mode": "fedora-static-read-only",
                }
            if addresses and expected_address not in addresses:
                raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_ADDRESS_MISMATCH")
        if time.monotonic() >= deadline:
            raise GuestIdentityError("SERVICE_BOOTSTRAP_STATIC_ADDRESS_UNAVAILABLE")
        time.sleep(2)
