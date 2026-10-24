"""Regresión de presentación, autenticación y equivalencia HTTP de Fase F."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver
from openapi_spec_validator import validate

from iacp.docs_ui import ASSETS
from iacp.main import create_app
from scripts.openapi_contract import transport_projection

ROOT = Path(__file__).resolve().parents[1]


def test_documentation_uses_local_versioned_assets_and_no_new_routes(client, auth):
    page = client.get('/docs', headers=auth)
    assert page.status_code == 200
    assert 'IACP Orchestrator' in page.text and 'lang="es"' in page.text
    assert 'cdn.jsdelivr' not in page.text and 'fastapi.tiangolo.com' not in page.text
    assert '/docs?asset=swagger-5.34.0.js' in page.text
    assert '/docs?asset=iacp-v1.svg' in page.text
    assert 'endpoint-search' in page.text
    for asset, (path, media) in ASSETS.items():
        url = '/docs?asset='+asset
        assert client.get(url).status_code == 401
        response = client.get(url, headers=auth)
        assert response.status_code == 200
        assert response.content == path.read_bytes()
        assert response.headers['Cache-Control'] == 'no-store'
    assert client.get('/docs?asset=../../security.py', headers=auth).status_code == 404
    assert not any('/assets' in r.path or '/static' in r.path for r in client.app.routes)


def test_admin_documentation_keeps_its_separate_schema(client, auth):
    page = client.get('/admin/docs', headers=auth)
    assert 'data-schema="/admin/openapi.json"' in page.text
    assert '/admin/docs?asset=swagger-5.34.0.js' in page.text
    assert client.get('/admin/docs?asset=iacp-v1.js').status_code == 401


def test_openapi_31_valid_and_wire_contract_exact():
    schema = create_app().openapi()
    validate(schema)
    before = json.loads((ROOT/'docs/validation/phase-f/swagger/OPENAPI_ANTES.json').read_text())
    assert transport_projection(before) == transport_projection(schema)
    assert schema['openapi'] == '3.1.0'
    assert schema['components']['securitySchemes'] == before['components']['securitySchemes']
    assert len(schema['paths']) == len(before['paths'])


def test_comparator_detects_validation_defaults_headers_security_and_codes():
    original = create_app().openapi()
    changes = [lambda s:s['components']['schemas']['InstanceCreate']['properties']['vcpus'].update(default=99),
               lambda s:s['components']['schemas']['InstanceCreate'].update(required=['name']),
               lambda s:s['paths']['/v1/instances']['post'].update(security=[]),
               lambda s:s['paths']['/v1/instances']['post']['responses'].pop('409'),
               lambda s:s['paths']['/v1/instances']['post']['parameters'][0].update(required=False)]
    for change in changes:
        altered=deepcopy(original);change(altered)
        assert transport_projection(original) != transport_projection(altered)


def test_every_editorial_example_matches_its_existing_schema():
    schema = create_app().openapi()
    resolver = RefResolver.from_schema(schema)
    count=0
    for methods in schema['paths'].values():
        for operation in methods.values():
            nodes=[operation.get('requestBody',{}),*operation.get('responses',{}).values()]
            for node in nodes:
                for media in node.get('content',{}).values():
                    for example in media.get('examples',{}).values():
                        if 'value' not in example:continue
                        Draft202012Validator(media['schema'],resolver=resolver).validate(example['value'])
                        count+=1
    assert count>=20


def test_client_assets_do_not_persist_credentials_or_use_online_validator(client,auth):
    script=client.get('/docs?asset=iacp-v1.js',headers=auth).text
    assert 'persistAuthorization: false' in script
    assert 'validatorUrl: null' in script
    assert 'localStorage' not in script and 'preauthorize' not in script
    public=create_app().openapi()
    assert not any('/provisioning/' in path for path in public['paths'])
    assert not any(method=='post' for path,methods in public['paths'].items()
                   if path.startswith('/v1/lab/policies') for method in methods)
