"""Semantic evidence scanning must detect non-header credentials and emit no value."""
import importlib.util
import json
from pathlib import Path


def scanner():
    spec = importlib.util.spec_from_file_location('audit_evidence_secrets',
        Path(__file__).parents[1] / 'scripts/audit_evidence_secrets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_json_and_jsonl_credentials_are_detected_without_echoing_values():
    scan = scanner().scan
    dummy = 'Z9fC3nV8qW2sR7yT4uI6oP0aB5dE1gHk'
    for data in [json.dumps({'nested': {'password': dummy}}),
                 '\n'.join([json.dumps({'event': 'start'}), json.dumps({'api_token': dummy})])]:
        result = scan(data)
        assert any('json-sensitive-field' in r[0] for r in result)
        assert dummy not in json.dumps(result)


def test_non_http_formats_and_fixture_classification():
    scan = scanner().scan
    assert any(r[0] == 'private-key' for r in scan('-----BEGIN OPENSSH PRIVATE KEY-----'))
    assert any(r[2] == 'synthetic-or-reference' for r in scan('{"password":"fixture-password"}'))
