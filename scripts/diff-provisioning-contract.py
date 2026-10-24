#!/usr/bin/env python3
"""Reproducible semantic diff against the pre-refactor HTTP contract."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from iacp.main import create_app


def compare(before, after, administrative):
    def operations(schema):
        return {(method.upper(), path): operation for path, methods in schema['paths'].items()
                for method, operation in methods.items()}
    old, new, admin = map(operations, [before, after, administrative])
    removed = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    changed = []
    for pair in sorted(set(old) & set(new)):
        for field in ['operationId', 'parameters', 'requestBody', 'responses', 'security']:
            if old[pair].get(field) != new[pair].get(field):
                changed.append({'method': pair[0], 'path': pair[1], 'field': field})
    old_schemas, new_schemas = (s['components']['schemas'] for s in [before, after])
    changed_schemas = {name: {'before': old_schemas[name], 'after': new_schemas[name]}
                       for name in sorted(old_schemas.keys() & new_schemas.keys())
                       if old_schemas[name] != new_schemas[name]}
    return {'before_version': before['info']['version'], 'after_version': after['info']['version'],
        'consumer_operations_before': len(old), 'consumer_operations_after': len(new),
        'removed_from_consumer_documentation': [{'method': m, 'path': p} for m, p in removed],
        'added_consumer_operations': [{'method': m, 'path': p} for m, p in added],
        'changed_existing_operations': changed, 'changed_shared_schemas': changed_schemas,
        'administrative_operations': [{'method': m, 'path': p} for m, p in sorted(set(admin) - set(new))],
        'removed_legacy_runtime_routes': [{'method': m, 'path': p} for m, p in sorted(old) if '/terraform' in p]}


def main():
    before = json.loads((ROOT / 'docs/contracts/openapi-before-provisioning.json').read_text())
    app = create_app()
    result = compare(before, app.openapi(), app.administrative_openapi())
    target = ROOT / 'docs/contracts/provisioning-openapi-diff.json'
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    print(target)


if __name__ == '__main__':
    main()
