"""Identidad exclusiva de admisión: HTTPS, GET y destinos de MNG acotados."""
import json
from http.client import HTTPException
import os
from pathlib import Path
import ssl
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPSHandler, ProxyHandler, build_opener

from ..terraform.observation import NoRedirect

IDENTITY = 'iacp-admission@pve!capacity-ro'
READ_PATHS = frozenset({'/nodes/mng01/status',
    '/nodes/mng01/storage/DS_CYBERFUEL/status', '/nodes/mng01/ceph/status',
    '/access/permissions'})


class CapacityError(Exception):
    """Códigos estables; nunca expone cabeceras o respuestas del proveedor."""


class CapacityReadOnly:
    @classmethod
    def configured(cls):
        try:
            path = Path(os.environ['IACP_ADMISSION_CREDENTIALS_FILE'])
            info = path.stat()
            if (path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027
                    or not 1 <= info.st_size <= 16384):
                raise ValueError()
            data = json.loads(path.read_text())
            if set(data) != {'endpoint', 'api_token'}:
                raise ValueError()
            endpoint, token = data['endpoint'], data['api_token']
            parsed = urlsplit(endpoint)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path not in {'', '/'}
                    or not isinstance(token, str) or '\n' in token or '\r' in token
                    or not 1 <= len(token) <= 1024
                    or token.partition('=')[0] != IDENTITY or not token.partition('=')[2]):
                raise ValueError()
            context = ssl.create_default_context(cafile=os.environ.get('IACP_ADMISSION_CA_FILE'))
            return cls(endpoint.rstrip('/'), token, build_opener(ProxyHandler({}),
                HTTPSHandler(context=context), NoRedirect()))
        except (KeyError, OSError, ValueError, TypeError, AttributeError):
            raise CapacityError('ADMISSION_CAPACITY_CREDENTIALS_UNAVAILABLE') from None

    def __init__(self, endpoint, token, opener):
        self.endpoint, self._token, self._opener = endpoint, token, opener

    def _request(self, method, path, parameters=None):
        if method != 'GET' or path not in READ_PATHS or parameters is not None:
            raise CapacityError('ADMISSION_CAPACITY_REQUEST_FORBIDDEN')
        request = Request(self.endpoint+'/api2/json'+path, method='GET',
            headers={'Authorization': 'PVEAPIToken='+self._token})
        try:
            with self._opener.open(request, timeout=10) as response:
                if response.status != 200:
                    raise CapacityError('ADMISSION_CAPACITY_HTTP_UNAVAILABLE')
                body = response.read(1024*1024+1)
            if len(body) > 1024*1024:
                raise ValueError()
            data = json.loads(body)['data']
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except HTTPError as e:
            raise CapacityError('ADMISSION_CAPACITY_PERMISSION_DENIED' if e.code in {401, 403}
                else 'ADMISSION_CAPACITY_HTTP_UNAVAILABLE') from None
        except (OSError, URLError, HTTPException, ValueError, TypeError, KeyError):
            raise CapacityError('ADMISSION_CAPACITY_HTTP_UNAVAILABLE') from None
