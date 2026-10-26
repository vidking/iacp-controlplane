"""Presentación local de Swagger; reutiliza autenticación y rutas existentes."""
from functools import lru_cache
from html import escape
import os
import json
from pprint import pformat
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

ROOT = Path(__file__).parent / 'static'
ASSETS = {
    'swagger-5.34.0.js': (ROOT/'swagger/5.34.0/swagger-ui-bundle.js', 'text/javascript'),
    'swagger-5.34.0.css': (ROOT/'swagger/5.34.0/swagger-ui.css', 'text/css'),
    'iacp-v1.css': (ROOT/'docs/iacp.css', 'text/css'),
    'ux-f1.css': (ROOT/'docs/ux-f1.css', 'text/css'),
    'iacp-v1.js': (ROOT/'docs/iacp.js', 'text/javascript'),
    'piloto-v1.json': (ROOT/'docs/piloto.json', 'application/json'),
    'iacp-v1.svg': (ROOT/'docs/iacp.svg', 'image/svg+xml'),
}


@lru_cache
def template():
    return (ROOT/'docs/index.html').read_text()


def documentation(request, version, *, administrative=False):
    asset = request.query_params.get('asset')
    if asset is not None:
        if asset not in ASSETS:
            raise HTTPException(404, 'Recurso documental no disponible')
        path, media_type = ASSETS[asset]
        return FileResponse(path, media_type=media_type)
    base = '/admin/docs' if administrative else '/docs'
    schema = '/admin/openapi.json' if administrative else '/openapi.json'
    html = template().replace('{{base}}', base).replace('{{schema}}', schema)
    pilot = json.loads((ROOT/'docs/piloto.json').read_text())
    body = json.dumps(pilot['request'], ensure_ascii=False, indent=2)
    command = (ROOT/'docs/piloto-creacion.sh').read_text().replace(
        '{{pilot_request_python}}', pformat(pilot['request'], width=88, sort_dicts=False))
    html = html.replace('{{pilot_json}}', escape(body)).replace('{{pilot_curl}}', escape(command))
    html = html.replace('{{pilot_response}}', escape(json.dumps(pilot['accepted'], indent=2)))
    html = html.replace('{{pilot_contract_note}}', '' if os.environ.get('IACP_CREATION_CONTRACT')=='commercial-g' else '<p class="auth-note">Entorno con contrato histórico: no ejecutar este recorrido comercial hasta completar la migración autorizada.</p>')
    html = html.replace('{{version}}', escape(version))
    html = html.replace('{{scope}}', 'Administración autorizada' if administrative else 'Integración CRM · MVP KVM')
    html = html.replace('{{creation_status}}', ('Contrato comercial G seleccionado; certificación física pendiente.'
        if os.environ.get('IACP_CREATION_CONTRACT')=='commercial-g' else
        'Revisión comercial candidata; creación todavía bloqueada en el runtime histórico.'))
    from alembic.script import ScriptDirectory
    from .migrations import alembic_config
    html=html.replace('{{schema_head}}',escape(ScriptDirectory.from_config(alembic_config()).get_current_head()))
    return HTMLResponse(html)
