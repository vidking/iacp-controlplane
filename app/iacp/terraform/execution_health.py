"""Barrera de almacenamiento y Ceph con la identidad propia del worker."""
import ssl
from urllib.parse import quote
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from .config import PlanError
from .observation import NoRedirect


def require_execution_health(env, expected, cancelled=lambda: False):
    """Solo GET; un error o una salud distinta de HEALTH_OK impiden ejecutar."""
    from ..lifecycle.proxmox import ProxmoxPower, PowerError
    phase = 'preflight.execution_health'
    try:
        client = ProxmoxPower(env['PROXMOX_VE_ENDPOINT'].rstrip('/'), env['PROXMOX_VE_API_TOKEN'],
            build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context(
                cafile=env.get('SSL_CERT_FILE'))), NoRedirect()))
        node = quote(expected['node_name'], safe='')
        stores = {expected['boot_disk']['datastore_id'], expected['cloudinit_datastore_id']}
        if expected.get('clone_datastore_id'):
            stores.add(expected['clone_datastore_id'])
        ceph = False
        for store in sorted(stores):
            if cancelled():
                raise PlanError('CANCELLED', phase=phase)
            status = client._request('GET', f'/nodes/{node}/storage/{quote(store, safe="")}/status')
            if (not isinstance(status, dict) or status.get('active') != 1
                    or status.get('enabled', 1) != 1 or not isinstance(status.get('type'), str)
                    or not status['type']):
                raise PlanError('EXECUTION_STORAGE_UNAVAILABLE', phase=phase)
            ceph = ceph or status['type'] in {'rbd', 'cephfs'}
        if ceph:
            if cancelled():
                raise PlanError('CANCELLED', phase=phase)
            status = client._request('GET', f'/nodes/{node}/ceph/status')
            if status.get('health', {}).get('status') != 'HEALTH_OK':
                raise PlanError('EXECUTION_CEPH_NOT_HEALTHY', phase=phase)
        if cancelled():
            raise PlanError('CANCELLED', phase=phase)
    except PlanError:
        raise
    except (PowerError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # No incorporar cuerpos remotos, credenciales ni excepciones a auditoría.
        raise PlanError('EXECUTION_HEALTH_UNAVAILABLE', phase=phase) from None
