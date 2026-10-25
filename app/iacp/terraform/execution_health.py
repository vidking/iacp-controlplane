"""Barrera operativa de los datastores KVM; no administra ni evalúa Ceph global."""
import ssl
from urllib.parse import quote
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

from .config import PlanError
from .observation import NoRedirect


def require_execution_health(env, expected, cancelled=lambda: False):
    """Solo GET; estado, espacio y lectura del backend deben ser comprobables."""
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
        for store in sorted(stores):
            if cancelled():
                raise PlanError('CANCELLED', phase=phase)
            path = f'/nodes/{node}/storage/{quote(store, safe="")}'
            status = client._request('GET', path + '/status')
            if (not isinstance(status, dict) or status.get('active') != 1
                    or status.get('enabled', 1) != 1 or not isinstance(status.get('type'), str)
                    or not status['type']):
                raise PlanError('EXECUTION_STORAGE_UNAVAILABLE', phase=phase)
            # No convertir avisos globales ajenos en un fallo KVM. Verificar el
            # backend concreto, sin escrituras ni credenciales CephX en la API.
            if (type(status.get('avail')) is not int or type(status.get('total')) is not int
                    or not 0 < status['avail'] <= status['total']
                    or 'images' not in str(status.get('content', '')).split(',')):
                raise PlanError('EXECUTION_STORAGE_UNAVAILABLE', phase=phase)
            if store in {expected['boot_disk']['datastore_id'], expected.get('clone_datastore_id')}:
                size = expected['boot_disk']['size_gb']
                if type(size) is not int or size < 1:
                    raise PlanError('EXECUTION_STORAGE_UNAVAILABLE', phase=phase)
                if status['avail'] < size * 1024**3:
                    raise PlanError('EXECUTION_STORAGE_CAPACITY_EXCEEDED', phase=phase)
            if cancelled():
                raise PlanError('CANCELLED', phase=phase)
            content = client._request('GET', path + '/content?content=images')
            if (not isinstance(content, list) or any(not isinstance(row, dict)
                    or not isinstance(row.get('volid'), str)
                    or not row['volid'].startswith(store + ':')
                    or row.get('content') != 'images' for row in content)):
                raise PlanError('EXECUTION_STORAGE_READ_FAILED', phase=phase)
        if cancelled():
            raise PlanError('CANCELLED', phase=phase)
    except PlanError:
        raise
    except (PowerError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # No incorporar cuerpos remotos, credenciales ni excepciones a auditoría.
        raise PlanError('EXECUTION_HEALTH_UNAVAILABLE', phase=phase) from None
