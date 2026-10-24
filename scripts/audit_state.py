#!/usr/bin/env python3
"""Readonly Wasabi audit from the existing IaC Worker; never writes S3 or SQL.

Only object headers, hashes/counts and references leave the worker. State bytes
may contain client passwords and are held only in memory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from iacp.db import get_session_factory
from iacp.models import Instance, TerraformExecution
from iacp.terraform.config import Backend, PlanError
from iacp.terraform.lifecycle import build_executor
from iacp.terraform.state import S3StateReader, StateSnapshot, LIMIT


def safe_error(error):
    if isinstance(error, PlanError):
        return {'status': 'unconfirmed', 'error_code': error.code}
    if isinstance(error, ClientError):
        code = error.response.get('Error', {}).get('Code', '')
        return {'status': 'unconfirmed', 'error_code': code if re.fullmatch(r'[A-Za-z0-9]{1,64}', code) else 'S3_READ_FAILED',
            'http_status': error.response.get('ResponseMetadata', {}).get('HTTPStatusCode')}
    return {'status': 'unconfirmed', 'error_code': 'S3_READ_FAILED'}


def resource_count(state):
    return sum(len(row.get('instances', [])) for row in state['resources'] if row.get('mode') == 'managed')


def inspect_state(reader, max_versions):
    current = reader.read(lambda: False)
    client = reader.client
    versions = []
    pages = client.get_paginator('list_object_versions').paginate(Bucket=reader.bucket, Prefix=reader.key,
        PaginationConfig={'MaxItems': max_versions + 1, 'PageSize': min(max_versions + 1, 100)})
    for page in pages:
        for row in page.get('Versions', []):
            if row['Key'] == reader.key:
                versions.append({'version_id': row['VersionId'], 'latest': row['IsLatest'],
                    'last_modified': row['LastModified'].isoformat(), 'size': row['Size']})
    locks = client.list_objects_v2(Bucket=reader.bucket, Prefix=reader.key + '.tflock', MaxKeys=2)
    latest = reader.read(lambda: False)
    return {**current.metadata(), 'resource_count': resource_count(current.state),
        'stable_during_audit': current.metadata() == latest.metadata(),
        'historical_versions': versions[:max_versions], 'history_truncated': len(versions) > max_versions,
        'active_lock_object': any(r['Key'] == reader.key+'.tflock' for r in locks.get('Contents', [])),
        'lockfile_required': True, 'writes_performed': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant', type=UUID, required=True)
    parser.add_argument('--max-versions', type=int, default=20, choices=range(1, 101))
    args = parser.parse_args()
    report = {'schema_version': 'iacp.state-audit.v1', 'tenant_id': str(args.tenant), 'states': [], 'buckets': [], 'writes_performed': 0}
    with tempfile.TemporaryDirectory() as temporary:
        env = build_executor(observation=True).environment(Path(temporary))
    with get_session_factory()() as session:
        rows = session.execute(select(Instance, TerraformExecution).join(TerraformExecution,
            TerraformExecution.instance_id == Instance.id).where(Instance.tenant_id == str(args.tenant))).all()
        seen = set()
        for instance, execution in rows:
            if not execution.allocation:
                report['states'].append({'instance_id': instance.id, 'status': 'allocation_unavailable'})
                continue
            backend = Backend.model_validate(execution.allocation['backend']).arguments(execution.state_key)
            reader = S3StateReader(backend, env)
            entry = {'instance_id': instance.id, 'inventory_status': instance.status, 'state_key': execution.state_key,
                'state_reference_present_in_postgresql': True}
            try:
                entry.update(inspect_state(reader, args.max_versions))
            except (ClientError, BotoCoreError, PlanError, OSError) as error:
                entry.update(safe_error(error))
            report['states'].append(entry)
            if reader.bucket not in seen:
                seen.add(reader.bucket)
                bucket = {'bucket': reader.bucket, 'observations': {}}
                for action in ('get_bucket_versioning', 'get_bucket_lifecycle_configuration', 'get_bucket_encryption', 'get_bucket_policy', 'get_bucket_acl'):
                    try:
                        value = getattr(reader.client, action)(Bucket=reader.bucket)
                        # Retention/policy details may identify principals. Publish
                        # only a digest and non-secret decision fields.
                        clean = {k:v for k,v in value.items() if k != 'ResponseMetadata'}
                        item = {'status': 'observed', 'sha256': hashlib.sha256(json.dumps(clean, default=str, sort_keys=True).encode()).hexdigest()}
                        if action == 'get_bucket_versioning':
                            item['versioning'] = value.get('Status', 'disabled')
                        if action == 'get_bucket_lifecycle_configuration':
                            item['rule_count'] = len(value.get('Rules', []))
                            item['enabled_rule_count'] = sum(r['Status'] == 'Enabled' for r in value.get('Rules', []))
                        bucket['observations'][action] = item
                    except (ClientError, BotoCoreError, OSError) as error:
                        bucket['observations'][action] = safe_error(error)
                report['buckets'].append(bucket)
            reader.close()
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
