#!/usr/bin/env python3
"""Auditar la identidad RO; probe de escritura vacío y explícito, nunca contra VMs."""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from iacp.services.capacity_readonly import CapacityReadOnly, CapacityError, IDENTITY


def audit(client, *, verify_write_denied=False):
    permissions = client._request('GET', '/access/permissions')
    # No enviar siquiera el probe vacío si el token muestra facultades de escritura.
    if any(not isinstance(privs, dict) or any(type(flag) is not int or flag not in (0, 1)
            or not name.endswith('.Audit') for name, flag in privs.items())
            for privs in permissions.values()):
        raise CapacityError('ADMISSION_CAPACITY_NON_READONLY_PRIVILEGES')
    reads = []
    for path in ['/nodes/mng01/status', '/nodes/mng01/storage/DS_CYBERFUEL/status', '/nodes/mng01/ceph/status']:
        client._request('GET', path)
        reads.append({'ruta': path, 'http': 200})
    result = {'lecturas': reads, 'escritura': 'NO_PROBADA'}
    if verify_write_denied:
        # PUT vacío: no establece opciones ni toca máquinas. Jamás enviar reboot,
        # shutdown, clone, DELETE, comandos de guest o cambios de ACL para probar RBAC.
        request = Request(client.endpoint+'/api2/json/nodes/mng01/config', data=b'', method='PUT',
            headers={'Authorization': 'PVEAPIToken='+client._token,
                'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with client._opener.open(request, timeout=10):
                raise CapacityError('ADMISSION_CAPACITY_WRITE_NOT_DENIED')
        except HTTPError as e:
            if e.code != 403:
                raise CapacityError('ADMISSION_CAPACITY_WRITE_PROBE_UNCONFIRMED') from None
        result['escritura'] = {'metodo': 'PUT', 'ruta': '/nodes/mng01/config', 'cuerpo': 'vacío', 'http': 403}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-write-denied', action='store_true')
    args = parser.parse_args()
    metadata = {'fecha': datetime.now(ZoneInfo('America/Costa_Rica')).isoformat(),
        'zona': 'America/Costa_Rica', 'identidad': IDENTITY}
    try:
        result = audit(CapacityReadOnly.configured(), verify_write_denied=args.verify_write_denied)
        print(json.dumps({**metadata, 'estado': 'PASS', **result})); return 0
    except CapacityError as e:
        print(json.dumps({**metadata, 'estado': 'BLOCKED', 'codigo': str(e)})); return 2
    except Exception:
        print(json.dumps({**metadata, 'estado': 'BLOCKED', 'codigo': 'ADMISSION_CAPACITY_AUDIT_UNCONFIRMED'})); return 2


if __name__ == '__main__':
    raise SystemExit(main())
