"""Identidad DHCP única para clones Ubuntu 24.04 del clúster MNG."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import time
from uuid import UUID
from urllib.parse import quote, urlencode

from api.protection import is_protected_vmid
from worker.golden import configured_pool_id
from worker.proxmox import ProxmoxClient, ProxmoxError


MODE = "ubuntu-2404-mng-dhcp-mac-v1"
MARKER = "/var/lib/iacp/guest-identity.json"
PENDING_MARKER = "/var/lib/iacp/guest-identity.pending.json"
NETWORK_FILE = "/etc/netplan/99-iacp-identity.yaml"
FAILURE_MARKER = "/var/lib/iacp/guest-identity.failure.json"


PROGRAM = r'''
import fcntl,hashlib,json,os,re,subprocess,sys,tempfile,uuid
from pathlib import Path
import yaml
identifier,mac,program_sha=sys.argv[1:]
failure=Path('/var/lib/iacp/guest-identity.failure.json')
def failure_hook(exc_type,exc,tb):
    match=re.search(r'IACP_GUEST_IDENTITY_STAGE=([A-Z0-9_]{1,64})',str(exc))
    stage=match.group(1) if match else 'UNEXPECTED'
    try:
        failure.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if failure.is_symlink(): return
        fd,temporary=tempfile.mkstemp(prefix='.iacp-failure-',dir=failure.parent)
        try:
            with os.fdopen(fd,'w') as output:
                os.fchmod(output.fileno(),0o600)
                json.dump({'instance_id':identifier,'mac_address':mac,'mode':'ubuntu-2404-mng-dhcp-mac-v1',
                           'program_sha256':program_sha,'stage':stage},output,sort_keys=True)
                output.write('\n');output.flush();os.fsync(output.fileno())
            os.replace(temporary,failure)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
    except OSError:
        pass
sys.excepthook=failure_hook
try:
    failure.unlink(missing_ok=True)
except OSError:
    raise RuntimeError('IACP_GUEST_IDENTITY_STAGE=FAILURE_MARKER_RESET')
assert str(uuid.UUID(identifier))==identifier and re.fullmatch(r'[0-9a-f]{2}(:[0-9a-f]{2}){5}',mac), 'IACP_GUEST_IDENTITY_STAGE=INPUT'
assert re.fullmatch(r'[0-9a-f]{64}',program_sha), 'IACP_GUEST_IDENTITY_STAGE=PROGRAM_HASH'
def cloud_init_completed(returncode,output,boot_finished):
    if not boot_finished or not re.search(r'(?m)^status:\s*done\s*$', output):
        return False
    if not re.search(r'(?m)^errors:\s*\[\]\s*$', output):
        return False
    if returncode == 0:
        return True
    if returncode != 2 or 'recoverable_errors:' not in output:
        return False
    recoverable = output.split('recoverable_errors:', 1)[1]
    lines = [line.strip() for line in recoverable.splitlines() if line.strip()]
    warnings = lines[1:]
    return bool(
        len(lines) > 1
        and lines[0] == 'DEPRECATED:'
        and all(line.startswith('- ') and 'deprecated' in line.lower() for line in warnings)
    )
release=Path('/etc/os-release').read_text()
assert 'ID=ubuntu\n' in release and 'VERSION_ID="24.04"' in release, 'IACP_GUEST_IDENTITY_STAGE=OS_RELEASE'
interfaces=[p for p in Path('/sys/class/net').glob('*/address') if p.read_text().strip().lower()==mac]
assert len(interfaces)==1, 'IACP_GUEST_IDENTITY_STAGE=MAC_INTERFACE'
ifindex=int((interfaces[0].parent/'ifindex').read_text())
lock=Path('/run/lock/iacp-guest-identity.lock')
lock.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
with lock.open('a') as stream:
    fcntl.flock(stream,fcntl.LOCK_EX)
    marker=Path('/var/lib/iacp/guest-identity.json')
    pending=Path('/var/lib/iacp/guest-identity.pending.json')
    network=Path('/etc/netplan/99-iacp-identity.yaml')
    def write(path,value,mode=0o600):
        assert not path.is_symlink()
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd,temporary=tempfile.mkstemp(prefix='.iacp-',dir=path.parent)
        try:
            with os.fdopen(fd,'w') as output:
                os.fchmod(output.fileno(),mode);output.write(value);output.flush();os.fsync(output.fileno())
            os.replace(temporary,path)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
    if marker.exists():
        evidence=json.loads(marker.read_text())
        assert evidence.get('instance_id')==identifier and evidence.get('mac_address')==mac, 'IACP_GUEST_IDENTITY_STAGE=MARKER_BINDING'
        assert evidence.get('mode')=='ubuntu-2404-mng-dhcp-mac-v1' and evidence.get('program_sha256')==program_sha, 'IACP_GUEST_IDENTITY_STAGE=MARKER_VERSION'
        assert hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()==evidence.get('machine_id_sha256'), 'IACP_GUEST_IDENTITY_STAGE=MACHINE_ID_HASH'
        assert hashlib.sha256(network.read_bytes()).hexdigest()==evidence.get('network_sha256'), 'IACP_GUEST_IDENTITY_STAGE=NETWORK_HASH'
        if evidence.get('ifindex')!=ifindex:
            evidence['ifindex']=ifindex
            write(marker,json.dumps(evidence,sort_keys=True)+'\n')
        sys.exit(0)
    cloud_init = subprocess.run(
        ['/usr/bin/cloud-init', 'status', '--wait', '--long'],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert cloud_init_completed(
        cloud_init.returncode,
        cloud_init.stdout or '',
        Path('/var/lib/cloud/instance/boot-finished').is_file(),
    ), 'IACP_GUEST_IDENTITY_STAGE=CLOUD_INIT'
    config=yaml.safe_load(Path('/etc/netplan/50-cloud-init.yaml').read_text())['network']
    assert config['version']==2 and set(config)<= {'version','renderer','ethernets'}, 'IACP_GUEST_IDENTITY_STAGE=NETPLAN_STRUCTURE'
    assert config.get('renderer','networkd')=='networkd' and len(config['ethernets'])==1, 'IACP_GUEST_IDENTITY_STAGE=NETPLAN_RENDERER'
    name,ethernet=next(iter(config['ethernets'].items()))
    assert re.fullmatch(r'[A-Za-z0-9_.-]{1,64}',name), 'IACP_GUEST_IDENTITY_STAGE=NETPLAN_INTERFACE_NAME'
    assert ethernet['match']['macaddress'].lower()==mac and ethernet.get('dhcp4') is True, 'IACP_GUEST_IDENTITY_STAGE=NETPLAN_MAC_DHCP'
    assert not any(ethernet.get(key) for key in ('addresses','gateway4','gateway6','routes')), 'IACP_GUEST_IDENTITY_STAGE=NETPLAN_STATIC_CONFIG'
    desired=json.dumps({'network':{'version':2,'ethernets':{name:{'dhcp-identifier':'mac'}}}},sort_keys=True)+'\n'
    if pending.exists():
        state=json.loads(pending.read_text())
        assert state.get('instance_id')==identifier and state.get('mac_address')==mac, 'IACP_GUEST_IDENTITY_STAGE=PENDING_BINDING'
        assert state.get('mode')=='ubuntu-2404-mng-dhcp-mac-v1' and state.get('program_sha256')==program_sha, 'IACP_GUEST_IDENTITY_STAGE=PENDING_VERSION'
        assert state.get('network_content')==desired and state.get('ifindex')==ifindex, 'IACP_GUEST_IDENTITY_STAGE=PENDING_NETWORK'
        machine=state['machine_id']
        assert re.fullmatch(r'[0-9a-f]{32}\n',machine), 'IACP_GUEST_IDENTITY_STAGE=PENDING_MACHINE_ID'
    else:
        machine=uuid.uuid4().hex+'\n'
        state={'instance_id':identifier,'mac_address':mac,'mode':'ubuntu-2404-mng-dhcp-mac-v1',
               'program_sha256':program_sha,'network_content':desired,'machine_id':machine,'ifindex':ifindex}
        write(pending,json.dumps(state,sort_keys=True)+'\n')
    if network.exists():
        assert network.read_text()==desired, 'IACP_GUEST_IDENTITY_STAGE=NETWORK_FILE'
    else:
        write(network,desired)
    write(Path('/etc/machine-id'),machine,0o444)
    dbus=Path('/var/lib/dbus/machine-id')
    if dbus.is_symlink():
        assert dbus.resolve()==Path('/etc/machine-id'), 'IACP_GUEST_IDENTITY_STAGE=DBUS_MACHINE_ID'
    elif dbus.exists():
        write(dbus,machine,0o444)
    try:
        subprocess.run(['/usr/sbin/netplan','generate'],check=True,capture_output=True,timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError('IACP_GUEST_IDENTITY_STAGE=NETPLAN_GENERATE')
    try:
        subprocess.run(['/usr/sbin/netplan','apply'],check=True,capture_output=True,timeout=60)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError('IACP_GUEST_IDENTITY_STAGE=NETPLAN_APPLY')
    evidence={'instance_id':identifier,'mac_address':mac,'mode':'ubuntu-2404-mng-dhcp-mac-v1',
              'program_sha256':program_sha,'machine_id_sha256':hashlib.sha256(machine.encode()).hexdigest(),
              'network_sha256':hashlib.sha256(desired.encode()).hexdigest(),'ifindex':ifindex}
    write(marker,json.dumps(evidence,sort_keys=True)+'\n')
    pending.unlink(missing_ok=True)
'''
PROGRAM_SHA256 = hashlib.sha256(PROGRAM.encode()).hexdigest()


def _guest_program_failure_stage(process: dict) -> str:
    """Extract only a fixed diagnostic code; never persist guest output."""

    encoded = process.get("err-data")
    if not isinstance(encoded, str) or not encoded:
        return "UNKNOWN"
    try:
        output = base64.b64decode(encoded[-16_384:], validate=False).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return "UNKNOWN"
    match = re.search(r"IACP_GUEST_IDENTITY_STAGE=([A-Z0-9_]{1,64})", output)
    return match.group(1) if match else "UNKNOWN"


def _guest_failure_marker_stage(
    client: ProxmoxClient,
    node: str,
    vmid: int,
    instance_id: str,
    mac: str,
) -> str:
    """Leer solo el código acotado escrito por el programa guest autenticado."""

    try:
        marker = json.loads(_guest_file(client, node, vmid, FAILURE_MARKER))
    except (GuestIdentityError, ProxmoxError, ValueError, TypeError):
        return "UNKNOWN"
    if not isinstance(marker, dict) or (
        marker.get("instance_id") != instance_id
        or marker.get("mac_address") != mac
        or marker.get("mode") != MODE
        or marker.get("program_sha256") != PROGRAM_SHA256
    ):
        return "UNKNOWN"
    stage = marker.get("stage")
    return stage if isinstance(stage, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", stage) else "UNKNOWN"


class GuestIdentityError(ProxmoxError):
    """La identidad DHCP del guest no pudo confirmarse; requiere reconciliación."""

    def __init__(self, code: str):
        super().__init__(code, uncertain=True)


def required(spec: dict, stack: str, vmid: int | None) -> bool:
    """Limitar la mutación a Ubuntu 24.04 DHCP en VM gestionadas de MNG."""

    try:
        numeric_vmid = int(vmid) if vmid is not None else 0
    except (TypeError, ValueError):
        return False
    return bool(
        stack == "mng01"
        and 30000 <= numeric_vmid <= 49999
        and not is_protected_vmid(numeric_vmid)
        and spec.get("image") == "ubuntu-2404"
        and spec.get("network_profile", "lab_dhcp") == "lab_dhcp"
        and spec.get("network_mode", "dhcp") == "dhcp"
    )


def _mac_from_config(config: dict, name: str) -> str:
    raw = config.get("net0")
    if (
        config.get("name") != name
        or config.get("template", 0)
        or not isinstance(raw, str)
        or config.get("ipconfig0") != "ip=dhcp"
    ):
        raise GuestIdentityError("GUEST_IDENTITY_BINDING_CHANGED")
    first = raw.split(",", 1)[0]
    model, separator, mac = first.partition("=")
    if not separator or model not in {"virtio", "e1000"} or not re.fullmatch(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", mac):
        raise GuestIdentityError("GUEST_IDENTITY_MAC_INVALID")
    return mac.lower()


def _guest_file(client: ProxmoxClient, node: str, vmid: int, path: str) -> str:
    data = client.guest_agent_file_read(node, vmid, path)
    if not isinstance(data, dict) or data.get("truncated"):
        raise GuestIdentityError("GUEST_IDENTITY_EVIDENCE_INVALID")
    content = data.get("content")
    if not isinstance(content, str):
        raise GuestIdentityError("GUEST_IDENTITY_EVIDENCE_INVALID")
    return content


def _matching_ipv4(interfaces: list[dict], mac: str) -> list[str]:
    result = []
    for interface in interfaces:
        if str(interface.get("hardware-address", "")).lower() != mac:
            continue
        for entry in interface.get("ip-addresses") or []:
            try:
                address = ipaddress.ip_address(str(entry.get("ip-address", "")))
            except ValueError:
                continue
            if address.version == 4 and not (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
                result.append(str(address))
    return list(dict.fromkeys(result))


def _observe(client: ProxmoxClient, node: str, vmid: int, instance_id: str, mac: str) -> dict | None:
    try:
        marker = json.loads(_guest_file(client, node, vmid, MARKER))
    except ProxmoxError as error:
        if error.status not in {400, 404, 500}:
            raise GuestIdentityError("GUEST_IDENTITY_AGENT_UNAVAILABLE") from error
        return None
    if not isinstance(marker, dict) or (
        marker.get("instance_id") != instance_id
        or marker.get("mac_address") != mac
        or marker.get("mode") != MODE
        or marker.get("program_sha256") != PROGRAM_SHA256
    ):
        raise GuestIdentityError("GUEST_IDENTITY_EVIDENCE_INVALID")
    machine_id = _guest_file(client, node, vmid, "/etc/machine-id")
    network_config = _guest_file(client, node, vmid, NETWORK_FILE)
    if (
        hashlib.sha256(machine_id.encode()).hexdigest() != marker.get("machine_id_sha256")
        or hashlib.sha256(network_config.encode()).hexdigest() != marker.get("network_sha256")
    ):
        raise GuestIdentityError("GUEST_IDENTITY_EVIDENCE_INVALID")
    if not isinstance(marker.get("ifindex"), int) or not 1 <= marker["ifindex"] <= 65535:
        raise GuestIdentityError("GUEST_IDENTITY_EVIDENCE_INVALID")
    interfaces = client.guest_agent_interfaces(node, vmid)
    addresses = _matching_ipv4(interfaces, mac)
    if not addresses:
        return None
    try:
        lease = dict(
            line.split("=", 1)
            for line in _guest_file(
                client,
                node,
                vmid,
                f"/run/systemd/netif/leases/{marker['ifindex']}",
            ).splitlines()
            if "=" in line
        )
    except ProxmoxError:
        return None
    client_id = lease.get("CLIENTID", "").lower().replace(":", "").replace("-", "")
    lease_address = lease.get("ADDRESS", "").split("/", 1)[0]
    if client_id != "01" + mac.replace(":", "") or lease_address not in addresses:
        return None
    return {"mac_address": mac, "ipv4": lease_address, "mode": MODE}


def _wait_for_pool_membership(
    client: ProxmoxClient,
    *,
    node: str,
    vmid: int,
    name: str,
    pool_id: str,
    heartbeat=None,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> None:
    """Confirmar por inventario y miembros del pool antes de usar Guest Agent."""

    wait_seconds = max(
        0.01,
        float(
            timeout
            if timeout is not None
            else os.getenv("IACP_GUEST_POOL_TIMEOUT", "30")
        ),
    )
    delay = max(
        0.01,
        float(
            poll_interval
            if poll_interval is not None
            else os.getenv("IACP_GUEST_IDENTITY_POLL_SECONDS", "2")
        ),
    )
    deadline = time.monotonic() + wait_seconds
    observed = False
    pool_path = f"/pools/{quote(pool_id, safe='')}"

    while time.monotonic() < deadline:
        if heartbeat is not None:
            heartbeat()
        try:
            inventory = client.inventory()
            pool = client.get(pool_path)
            observed = True
        except ProxmoxError as error:
            if error.status in {401, 403}:
                raise GuestIdentityError("GUEST_IDENTITY_PERMISSION_REQUIRED") from error
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            continue

        physical = [
            item for item in inventory
            if isinstance(item, dict) and str(item.get("vmid", "")) == str(vmid)
        ]
        members = pool.get("members") if isinstance(pool, dict) else None
        members = members if isinstance(members, list) else []
        pooled = [
            item for item in members
            if isinstance(item, dict)
            and str(item.get("vmid", "")) == str(vmid)
            and item.get("type") == "qemu"
        ]
        if (
            isinstance(pool, dict)
            and pool.get("poolid") == pool_id
            and len(physical) == 1
            and physical[0].get("name") == name
            and physical[0].get("node") == node
            and len(pooled) == 1
            and pooled[0].get("name") == name
            and pooled[0].get("node") == node
        ):
            return
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))

    code = "GUEST_IDENTITY_POOL_MISMATCH" if observed else "GUEST_IDENTITY_POOL_OBSERVATION_FAILED"
    raise GuestIdentityError(code)


def _ensure(
    client: ProxmoxClient,
    *,
    node: str,
    vmid: int,
    name: str,
    instance_id: str,
    heartbeat=None,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> dict:
    """Inicializar una vez y esperar el lease DHCP identificado por la MAC."""

    if is_protected_vmid(vmid) or not 30000 <= int(vmid) <= 49999:
        raise GuestIdentityError("GUEST_IDENTITY_VM_PROTECTED")
    try:
        if str(UUID(instance_id)) != instance_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as error:
        raise GuestIdentityError("GUEST_IDENTITY_BINDING_CHANGED") from error

    config = client.vm_config(node, vmid)
    mac = _mac_from_config(config, name)
    pool_id = configured_pool_id()
    _wait_for_pool_membership(
        client,
        node=node,
        vmid=vmid,
        name=name,
        pool_id=pool_id,
        heartbeat=heartbeat,
        timeout=timeout,
        poll_interval=poll_interval,
    )

    # Proxmox reports ACLs inherited from the VM's resource pool at the pool
    # path. Querying /vms/{vmid} alone returns zeroes for these inherited
    # privileges even though QEMU Guest Agent calls are authorized.
    permission_path = f"/pool/{pool_id}"
    permissions = client.get("/access/permissions?" + urlencode({"path": permission_path}))
    grants = permissions.get(permission_path, {}) if isinstance(permissions, dict) else {}
    if grants.get("VM.GuestAgent.Unrestricted") not in {1, True}:
        raise GuestIdentityError("GUEST_IDENTITY_PERMISSION_REQUIRED")

    max_wait = timeout if timeout is not None else max(60.0, float(os.getenv("IACP_GUEST_IDENTITY_TIMEOUT", "360")))
    delay = poll_interval if poll_interval is not None else max(0.25, float(os.getenv("IACP_GUEST_IDENTITY_POLL_SECONDS", "2")))
    deadline = time.monotonic() + max_wait

    while time.monotonic() < deadline:
        if heartbeat is not None:
            heartbeat()
        try:
            client.guest_agent_info(node, vmid)
            break
        except ProxmoxError as error:
            if error.status in {401, 403}:
                raise GuestIdentityError("GUEST_IDENTITY_PERMISSION_REQUIRED") from error
            time.sleep(delay)
    else:
        raise GuestIdentityError("GUEST_IDENTITY_AGENT_UNAVAILABLE")

    pid = None
    try:
        result = client.guest_agent_exec(node, vmid, ["/usr/bin/python3", "-c", PROGRAM, instance_id, mac, PROGRAM_SHA256])
        pid = result.get("pid") if isinstance(result, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            raise GuestIdentityError("GUEST_IDENTITY_PID_INVALID")
    except ProxmoxError as error:
        if error.status in {401, 403}:
            raise GuestIdentityError("GUEST_IDENTITY_PERMISSION_REQUIRED") from error
        if not error.uncertain:
            raise GuestIdentityError("GUEST_IDENTITY_DISPATCH_FAILED") from error

    while time.monotonic() < deadline:
        if heartbeat is not None:
            heartbeat()
        if pid is not None:
            try:
                process = client.guest_agent_exec_status(node, vmid, pid)
            except ProxmoxError as error:
                if error.status in {401, 403}:
                    raise GuestIdentityError("GUEST_IDENTITY_PERMISSION_REQUIRED") from error
                process = None
            if isinstance(process, dict):
                if not process.get("exited"):
                    time.sleep(delay)
                    continue
                if process.get("exitcode") != 0:
                    stage = _guest_program_failure_stage(process)
                    if stage == "UNKNOWN":
                        stage = _guest_failure_marker_stage(client, node, vmid, instance_id, mac)
                    raise GuestIdentityError(f"GUEST_IDENTITY_PROGRAM_FAILED_{stage}")
                pid = None
        observed = _observe(client, node, vmid, instance_id, mac)
        if observed is not None:
            return observed
        time.sleep(delay)
    raise GuestIdentityError("GUEST_IDENTITY_OBSERVATION_TIMEOUT")


def ensure(
    client: ProxmoxClient,
    *,
    node: str,
    vmid: int,
    name: str,
    instance_id: str,
    heartbeat=None,
    timeout: float | None = None,
    poll_interval: float | None = None,
) -> dict:
    """Convertir cualquier fallo de identidad en recuperación fail-closed."""

    try:
        return _ensure(
            client,
            node=node,
            vmid=vmid,
            name=name,
            instance_id=instance_id,
            heartbeat=heartbeat,
            timeout=timeout,
            poll_interval=poll_interval,
        )
    except GuestIdentityError:
        raise
    except ProxmoxError as error:
        code = "GUEST_IDENTITY_PERMISSION_REQUIRED" if error.status in {401, 403} else "GUEST_IDENTITY_PROXMOX_UNAVAILABLE"
        raise GuestIdentityError(code) from error
    except Exception as error:
        raise GuestIdentityError("GUEST_IDENTITY_INITIALIZATION_FAILED") from error
