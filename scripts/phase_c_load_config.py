"""Derive a private read-only load config with the Phase C safety limits."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    value = json.loads(args.source.read_text())
    if value.get('host') != 'https://iac.cyberfuel.com':
        raise SystemExit('MNG endpoint required')
    value.update(scenario='readonly', rps=5, max_requests=1600,
                 max_vms=3, max_concurrent_creations=2, max_failures=5)
    if not value.get('identities') or any('token_file' not in i for i in value['identities']):
        raise SystemExit('Existing private credential-file references required')
    # Reject inline secrets; preserve only the existing credential-file references.
    value['identities'] = [{k: i[k] for k in ('name', 'tenant_id', 'token_file', 'profile', 'policy_id') if k in i}
                           for i in value['identities']]
    fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    print('Private config created: readonly, 5 RPS, 1600 request cap; no credential values exported')


if __name__ == '__main__':
    main()
