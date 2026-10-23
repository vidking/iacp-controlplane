"""Observación Proxmox nativa: solo GET, misma identidad y CA que Terraform."""
import json
from http.client import HTTPException
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, HTTPSHandler

from .config import Cancelled, PlanError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Nunca reenviar Authorization a un destino diferente.
        return None


def proxmox_resource(env, expected, cancelled):
    """Un inventario filtrado por ACL no prueba ausencia en el clúster."""
    phase = "observe.proxmox"
    try:
        endpoint = env["PROXMOX_VE_ENDPOINT"].rstrip("/")
        token = env["PROXMOX_VE_API_TOKEN"]
        context = ssl.create_default_context(cafile=env.get("SSL_CERT_FILE"))
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())

        def get(path):
            if cancelled():
                raise Cancelled()
            request = Request(endpoint + "/api2/json" + path,
                              headers={"Authorization": "PVEAPIToken=" + token}, method="GET")
            with opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise PlanError("PROXMOX_OBSERVATION_FAILED", phase=phase, http_status=response.status)
                body = response.read(4 * 1024 * 1024 + 1)
            if cancelled():
                raise Cancelled()
            if len(body) > 4 * 1024 * 1024:
                raise PlanError("OUTPUT_LIMIT_EXCEEDED", phase=phase)
            return json.loads(body)["data"]

        inventory = get("/cluster/resources?type=vm")
        if not isinstance(inventory, list) or any(
                not isinstance(row, dict) or "vmid" not in row or row.get("type") not in {"qemu", "lxc"}
                or not re.fullmatch(r"[1-9][0-9]*", str(row["vmid"]))
                for row in inventory):
            raise ValueError("Invalid inventory")
        matches = [row for row in inventory if str(row["vmid"]) == str(expected["vm_id"])]
        if not matches:
            # La API devuelve si VM.Audit se propaga; 0 no cubre /vms/<id>.
            permissions = get("/access/permissions?path=/vms")
            if permissions.get("/vms", {}).get("VM.Audit") != 1:
                raise PlanError("PROXMOX_OBSERVATION_SCOPE_INSUFFICIENT", phase=phase)
            return {"present": False}
        if len(matches) != 1:
            raise ValueError("Ambiguous VMID")
        vm = matches[0]
        if (vm["type"] != "qemu" or vm["node"] != expected["node_name"]
                or vm["name"] != expected["vm_name"] or vm.get("template", 0)
                or vm["status"] not in {"running", "stopped"}):
            raise PlanError("OBSERVED_RESOURCE_MISMATCH", phase=phase)
        config = get(f'/nodes/{quote(expected["node_name"], safe="")}/qemu/{expected["vm_id"]}/config')
        if config["name"] != expected["vm_name"] or config.get("template", 0):
            raise PlanError("OBSERVED_RESOURCE_MISMATCH", phase=phase)
        if config.get("lock"):
            raise PlanError("RESOURCE_OPERATION_IN_PROGRESS", phase=phase)
        if expected.get('network_mode')=='dhcp':
            nic=dict(part.split('=',1) for part in config.get('net0','').split(',') if '=' in part)
            ip=dict(part.split('=',1) for part in config.get('ipconfig0','').split(',') if '=' in part)
            vlan=expected['vlan_id']
            if (nic.get('bridge')!=expected['network_bridge'] or 'virtio' not in nic
                    or (vlan is None and ('tag' in nic or 'trunks' in nic))
                    or (vlan is not None and nic.get('tag')!=str(vlan))
                    or ip.get('ip')!='dhcp' or 'gw' in ip):
                raise PlanError('DHCP_NATIVE_CONFIGURATION_MISMATCH',phase=phase)
        return {"present": True, "power_state": vm["status"]}
    except PlanError:
        raise
    except HTTPError as error:
        raise PlanError("PROXMOX_OBSERVATION_FAILED", phase=phase, http_status=error.code) from None
    except (OSError, URLError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        raise PlanError("PROXMOX_OBSERVATION_FAILED", phase=phase) from None


def require_clone_permissions(env, expected):
    """Check effective permissions before planning/consuming a DHCP mutation.

    Observation/recovery never calls this function: it needs no Clone permission.
    """
    from ..lifecycle.proxmox import ProxmoxPower, PowerError, granted
    try:
        client = ProxmoxPower(env['PROXMOX_VE_ENDPOINT'].rstrip('/'),env['PROXMOX_VE_API_TOKEN'],
            build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=env.get('SSL_CERT_FILE'))),NoRedirect()))
        checks=[('/vms/'+str(expected['template_vm_id']),'VM.Clone','VM_CLONE_PERMISSION_REQUIRED'),
                ('/vms/'+str(expected['vm_id']),'VM.Allocate','VM_ALLOCATE_PERMISSION_REQUIRED')]
        stores={expected['cloudinit_datastore_id'],expected['boot_disk']['datastore_id']}
        if expected.get('clone_datastore_id'):stores.add(expected['clone_datastore_id'])
        checks += [('/storage/'+quote(store,safe=''),'Datastore.AllocateSpace','DATASTORE_ALLOCATION_PERMISSION_REQUIRED') for store in sorted(stores)]
        checks.append(('/sdn/zones/localnetwork/'+quote(expected['network_bridge'],safe=''),'SDN.Use','SDN_USE_PERMISSION_REQUIRED'))
        for path, privilege, code in checks:
            effective=client._request('GET','/access/permissions?path='+quote(path,safe='/'))
            if not granted(effective.get(path,{}),privilege):raise PlanError(code,phase='preflight.clone_permissions')
    except PowerError:
        raise PlanError('CLONE_PERMISSION_CHECK_UNAVAILABLE',phase='preflight.clone_permissions') from None
