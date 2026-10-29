"""Real PostgreSQL, Redis/Celery, HTTPS PVE and S3 protocol fixtures.

Only the offline bpg plan is stubbed here; its orchestration and policy have
separate unit tests and the certified source's real-provider plan matrix.
"""
import pytest
from sqlalchemy import inspect, select, text

from iacp.lifecycle import certify as certification
from iacp.models import BOOTSTRAP_TENANT_ID, Operation, PowerOwnershipCertificate
from tests.helpers import upgrade
from tests.integration.test_power import sessions
from tests.power_helpers import managed_instance
from tests.test_power import request
from tests.test_power_certification import historical, history

pytestmark = pytest.mark.integration


def test_historical_certificate_is_audit_only_and_does_not_grant_lifecycle(
        client, auth, historical, sessions):
    expected, executor, runner, fake, planner = historical
    before = history(sessions)
    result = certification.certify(expected, sessions, executor, record=True, client=fake)
    assert result["validated"] and result["recorded"]
    assert history(sessions) == before
    with sessions() as session:
        assert session.scalar(select(PowerOwnershipCertificate)) is not None
    # El certificado histórico no sustituye al vínculo técnico único ni
    # habilita Lifecycle cuando la evidencia del vínculo no coincide.
    assert request(client, auth, expected.instance_id).status_code == 409
    assert not any(call[0] == "submit" for call in fake.calls)


def test_0006_preserves_0005_rows_and_never_backfills_certificates(pg_engine, monkeypatch):
    monkeypatch.setenv("IACP_KVM_LIFECYCLE_ENABLED", "0")
    from sqlalchemy.orm import sessionmaker
    from uuid import uuid4
    from iacp.lifecycle.execution import execute_power
    from iacp.lifecycle.ownership import LEGACY_TERRAFORM_POWER_SOURCE, KVM_POWER_SOURCE_REVISION
    from iacp.models import Job, Operation, PowerExecution, TerraformApply, TerraformExecution
    from tests.power_helpers import FakePower
    upgrade(pg_engine, "0005_kvm_lifecycle")
    factory = sessionmaker(bind=pg_engine, expire_on_commit=False)
    identifier = managed_instance(factory, BOOTSTRAP_TENANT_ID)
    operation_id = str(uuid4())
    with factory() as session:
        session.get(TerraformExecution, identifier).source_revision = LEGACY_TERRAFORM_POWER_SOURCE
        approval = session.scalar(select(TerraformApply))
        from sqlalchemy import MetaData, Table
        from iacp.models import utcnow
        operation_table = Table('operations', MetaData(), autoload_with=session.connection())
        session.execute(operation_table.insert().values(id=operation_id, instance_id=identifier, tenant_id=BOOTSTRAP_TENANT_ID,
            kind="instance.power.start", status="queued", idempotency_key="pre-0006", request_hash="a" * 64, requested_by="fixture",
            created_at=utcnow(), updated_at=utcnow()))
        session.flush()
        session.add(Job(id=str(uuid4()), operation_id=operation_id, tenant_id=BOOTSTRAP_TENANT_ID,
                        operation="instance.power.start", status="queued"))
        # Use the actual 0005 columns; the model's new nullable FK is absent here.
        session.execute(PowerExecution.__table__.insert().values(operation_id=operation_id, instance_id=identifier,
            action="start", status="queued", phase="queued", vmid=30001, node="mng01", name="fixture-kvm",
            approval_id=approval.id, ownership_revision=KVM_POWER_SOURCE_REVISION))
        session.commit()
    tables = {name: [row["name"] for row in inspect(pg_engine).get_columns(name)]
              for name in inspect(pg_engine).get_table_names() if name != "alembic_version"}
    def snapshot():
        with pg_engine.connect() as connection:
            return {name: connection.execute(text('SELECT ' + ','.join('"' + col + '"' for col in columns)
                + f' FROM "{name}"')).all() for name, columns in tables.items()}
    before = snapshot()
    upgrade(pg_engine)
    upgrade(pg_engine)
    assert snapshot() == before
    with pg_engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM power_ownership_certificates")) == 0
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0015_technical_access"
        assert connection.scalar(text("SELECT ownership_certificate_id FROM power_executions")) is None
    native = FakePower()
    execute_power(operation_id, factory, native)
    assert not native.calls
    with factory() as session:
        assert session.get(Operation, operation_id).error_code == "POWER_LIFECYCLE_DISABLED"
