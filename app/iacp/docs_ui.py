"""Presentación local de Swagger; reutiliza autenticación y rutas existentes."""
from functools import lru_cache
from html import escape
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse

ROOT = Path(__file__).parent / 'static'
ASSETS = {
    'swagger-5.34.0.js': (ROOT/'swagger/5.34.0/swagger-ui-bundle.js', 'text/javascript'),
    'swagger-5.34.0.css': (ROOT/'swagger/5.34.0/swagger-ui.css', 'text/css'),
    'iacp-v1.css': (ROOT/'docs/iacp.css', 'text/css'),
    'iacp-v1.js': (ROOT/'docs/iacp.js', 'text/javascript'),
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
    html = html.replace('{{version}}', escape(version))
    html = html.replace('{{scope}}', 'Administración autorizada' if administrative else 'Integración CRM · MVP KVM')
    return HTMLResponse(html)
