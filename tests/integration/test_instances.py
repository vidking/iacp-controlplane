from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

from alembic import command
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import sessionmaker

from iacp.executors import ExecutionResult
from iacp.migrations import alembic_config
from iacp.models import BOOTSTRAP_TENANT_ID, Instance, Job, Operation, OperationEvent
from iacp.schemas import InstanceCreate
from iacp.security import Principal
from iacp.services.execution import execute_instance
from iacp.services.instances import Conflict, create_instance
from tests.helpers import legacy_snapshot, upgrade

pytestmark = pytest.mark.integration


def test_0002_upgrades_versioned_0001_without_changing_existing_jobs(pg_engine):
    upgrade(pg_engine, "0001_bootstrap_jobs")
    with pg_engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO jobs (id, operation, status, message, created_at, updated_at)
            VALUES ('legacy-completed', 'smoke', 'completed', 'preserved', now(), now()),
                   ('legacy-queued', 'smoke', 'queued', '', now(), now())
        """))
        before = legacy_snapshot(connection)
    upgrade(pg_engine)
    upgrade(pg_engine)
    with pg_engine.begin() as connection:
        assert legacy_snapshot(connection) == before
        assert connection.execute(text("SELECT tenant_id, operation_id FROM jobs")).all() == [(None, None), (None, None)]
        config = alembic_config()
        config.attributes["connection"] = connection
        command.check(config)


@pytest.mark.parametrize("different_payload", [False, True])
def test_database_arbitrates_concurrent_idempotent_creation(pg_engine, different_payload):
    upgrade(pg_engine)
    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    principal = Principal(BOOTSTRAP_TENANT_ID, "concurrent-test", "operator")
    barrier = Barrier(2)
    published = []

    def submit(number):
        with sessions() as session:
            def synchronize(session, context, instances):
                if any(isinstance(item, Instance) for item in session.new):
                    barrier.wait(timeout=10)

            event.listen(session, "before_flush", synchronize)
            spec = InstanceCreate(name=f"concurrent-{number if different_payload else 0}", image="simulation-test")
            try:
                return create_instance(session, principal, spec, "concurrent-key", published.append)
            except Conflict:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (0, 1)))
    succeeded = [result for result in results if result is not None]
    assert len(succeeded) == (1 if different_payload else 2)
    assert len({result.job_id for result in succeeded}) == 1
    assert len(published) == 1
    with sessions() as session:
        for model in (Instance, Operation, Job, OperationEvent):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_duplicate_concurrent_delivery_executes_once(pg_engine):
    upgrade(pg_engine)
    sessions = sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    principal = Principal(BOOTSTRAP_TENANT_ID, "concurrent-test", "operator")
    with sessions() as session:
        accepted = create_instance(session, principal, InstanceCreate(name="delivery", image="simulation-test"),
                                   "delivery-key", lambda job: None)
    barrier = Barrier(2)
    executor = Mock()
    executor.create.return_value = ExecutionResult({"simulated": True})

    def deliver(number):
        barrier.wait(timeout=10)
        execute_instance(accepted.job_id, sessions, executor)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(deliver, (0, 1)))
    executor.create.assert_called_once()
    with sessions() as session:
        assert session.get(Job, accepted.job_id).status == "completed"
        assert session.scalar(select(func.count()).select_from(OperationEvent)) == 3
