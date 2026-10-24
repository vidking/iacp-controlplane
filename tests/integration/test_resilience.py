"""Phase C failures confined to disposable PostgreSQL, Redis and Celery."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from iacp.main import create_app
from iacp.terraform.locks import instance_lock
from iacp.terraform.config import PlanError
from iacp.terraform.runner import CommandRunner
from iacp.terraform.state import S3StateReader
from tests.s3_fixture import versioned_s3
from tests.helpers import upgrade

pytestmark = pytest.mark.integration


def test_c05_temporary_state_outage_is_not_absence_and_retry_is_readonly():
    data = json.dumps({'version': 4, 'lineage': 'phase-c', 'serial': 1, 'resources': []}).encode()
    with versioned_s3(data) as store:
        backend = {'bucket': store.bucket, 'key': store.key, 'region': 'us-east-1',
                   'endpoints': {'s3': store.endpoint}}
        reader = S3StateReader(backend, {'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture'})
        try:
            store.state_available = False
            with pytest.raises(PlanError, match='STATE_BACKEND_READ_FAILED'):
                reader.read(lambda: False)
            failures = len(store.requests)
            assert failures == 2  # SDK total_max_attempts includes the initial request.
            store.state_available = True
            assert reader.read(lambda: False).state['serial'] == 1
            assert store.data == data and store.version == 1
            assert all(method == 'GET' for method, path in store.requests)
        finally:
            reader.close()


def test_c06_occupied_native_state_lock_is_preserved(tmp_path):
    data = json.dumps({'version': 4, 'terraform_version': '1.16.6', 'lineage': 'phase-c',
                       'serial': 1, 'outputs': {}, 'resources': []}).encode()
    held = json.dumps({'ID': '00000000-0000-4000-8000-000000000006', 'Operation': 'OperationTypeApply',
                       'Who': 'fixture-owner', 'Info': 'phase-c-held', 'Version': '1.16.6',
                       'Created': '2026-10-24T00:00:00Z', 'Path': 'fixture-state/terraform.tfstate'}).encode()
    (tmp_path / 'main.tf').write_text('terraform {\n backend "s3" {}\n}\nresource "terraform_data" "fixture" { input = "phase-c" }\n')
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path), 'TF_DATA_DIR': str(tmp_path / 'data'),
           'TF_INPUT': '0', 'TF_IN_AUTOMATION': '1', 'CHECKPOINT_DISABLE': '1',
           'AWS_EC2_METADATA_DISABLED': 'true', 'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture'}
    runner = CommandRunner(timeout=15)
    with versioned_s3(data) as store:
        backend = {'bucket': store.bucket, 'key': store.key, 'region': 'us-east-1',
                   'endpoints': {'s3': store.endpoint}, 'use_lockfile': True, 'use_path_style': True,
                   'skip_credentials_validation': True, 'skip_requesting_account_id': True,
                   'skip_metadata_api_check': True}
        (tmp_path / 'backend.json').write_text(json.dumps(backend))
        runner.run(['init', '-input=false', '-no-color', '-reconfigure', '-backend-config=backend.json'],
                   tmp_path, env, lambda: False)
        store.lock = held
        with pytest.raises(PlanError, match='TERRAFORM_COMMAND_FAILED'):
            runner.run(['plan', '-refresh-only', '-input=false', '-no-color', '-lock-timeout=1s'],
                       tmp_path, env, lambda: False)
        assert store.lock == held and store.data == data and store.version == 1
        assert ('DELETE', store.state_path + '.tflock') not in store.requests
        # Fixture owner releases its own lock; IACP performs no automatic unlock.
        store.lock = None


def test_c04_lost_postgres_lock_connection_fails_closed_and_releases_lock(pg_engine):
    identifier = '00000000-0000-0000-0000-000000000004'
    key = int.from_bytes(hashlib.sha256(identifier.encode()).digest()[:8], 'big', signed=True)
    with pytest.raises(SQLAlchemyError):
        with instance_lock(pg_engine, identifier) as heartbeat:
            with pg_engine.connect() as observer:
                # This database is a unique iacp_test_* fixture, never MNG.
                pid = observer.scalar(text("SELECT pid FROM pg_locks WHERE locktype='advisory' "
                                           "AND classid::bigint=:high AND objid::bigint=:low"),
                                      {'high': (key >> 32) & 0xffffffff, 'low': key & 0xffffffff})
                assert pid is not None
                assert observer.scalar(text('SELECT pg_terminate_backend(:pid)'), {'pid': pid})
            heartbeat()
    # PostgreSQL released the dead session's lock; no force-unlock is used.
    with instance_lock(pg_engine, identifier) as heartbeat:
        heartbeat()


def test_c12_pending_jobs_complete_after_prefork_worker_restart(pg_engine, runtime, tmp_path):
    from iacp.worker import celery_app
    upgrade(pg_engine)
    celery_app.conf.broker_url = os.environ['CELERY_BROKER_URL']
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PYTHONPATH': f'{root / "app"}:{root}'}
    worker = None
    with TestClient(create_app()) as client, (tmp_path / 'worker.log').open('w') as log:
        try:
            for number in range(2):
                accepted = client.post('/v1/jobs/smoke', headers=runtime)
                assert accepted.status_code == 202
                job_id = accepted.json()['job_id']
                assert client.get('/v1/jobs/' + job_id, headers=runtime).json()['status'] == 'queued'
                worker = subprocess.Popen([
                    sys.executable, '-m', 'celery', '-A', 'iacp.worker:celery_app', 'worker',
                    '--pool=prefork', '--concurrency=1', '--loglevel=ERROR',
                    '--without-gossip', '--without-mingle', '--without-heartbeat',
                ], env=env, stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if client.get('/v1/jobs/' + job_id, headers=runtime).json()['status'] == 'completed':
                        break
                    assert worker.poll() is None
                    time.sleep(.1)
                else:
                    pytest.fail('Pending job did not complete after worker startup')
                worker.terminate()
                worker.wait(timeout=10)
                worker = None
                assert client.get('/v1/jobs/' + job_id, headers=runtime).json()['status'] == 'completed'
        finally:
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.wait(timeout=5)
            celery_app.close()
