#!/usr/bin/env python3
"""Readonly demo contract gate. Refuse a partial destructive demo before creation."""
import argparse
import json
from pathlib import Path
import ssl
import time
from smoke_public import private_secret, request

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--manifest', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
m = json.loads(a.manifest.read_text())
token = private_secret(Path(m['operator_token_file']))
cookie = private_secret(Path(m['gateway_cookie_file'])) if m.get('identity_mode', 'gateway') == 'gateway' else None
started = time.monotonic()
status, _, body = request('/openapi.json', token=token, cookie=cookie, context=ssl.create_default_context(cafile=m.get('api_ca_file')))
if status != 200:
    raise SystemExit('Authorized API contract unavailable; no writes submitted')
paths = json.loads(body)['paths']
checks = {'create': 'post' in paths.get('/v1/instances', {}),
          'read': 'get' in paths.get('/v1/instances/{instance_id}', {}),
          'start': 'post' in paths.get('/v1/instances/{instance_id}/power/start', {}),
          'shutdown': 'post' in paths.get('/v1/instances/{instance_id}/power/shutdown', {}),
          'delete': 'delete' in paths.get('/v1/instances/{instance_id}', {})}
report = {'complete_crud_supported': all(checks.values()), 'contract': checks, 'writes_submitted': False,
          'operation_ids': [], 'elapsed_seconds': round(time.monotonic() - started, 3),
          'vm106_protected': True, 'next_step': 'Use only existing reviewed endpoints and a separately approved disposable allocation'}
with a.output.open('x') as stream:
    a.output.chmod(0o600); json.dump(report, stream, indent=2); stream.write('\n')
print(json.dumps(report))
raise SystemExit(0 if report['complete_crud_supported'] else 2)
