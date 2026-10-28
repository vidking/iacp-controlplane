"""Bound list-query work while preserving full, tenant-scoped operation payloads."""
from uuid import uuid4

from sqlalchemy import event, select

from iacp.models import (BOOTSTRAP_TENANT_ID, DeletionExecution, GuestInitialization,
                         NetworkDiscovery, Operation, OperationEvent, PowerExecution,
                         TerraformApply)
from tests.power_helpers import managed_instance
from tests.test_instances import identities


def test_list_queries_are_bounded_and_foreign_tenant_rows_stay_excluded(
        client, identities, engine, sessions):
    tenant_a = identities['tenant-a-operator']['headers']
    tenant_b = identities['tenant-b-operator']['headers']
    instance_ids = [
        session_id for session_id in (
            managed_instance(sessions, identities['tenant-a-operator']['tenant_id'],
                             vmid=30030 + number, name=f'operation-a-{number}')
            for number in range(12)
        )
    ]
    for number in range(3):
        managed_instance(sessions, identities['tenant-b-operator']['tenant_id'],
                         vmid=30050 + number, name=f'operation-b-{number}')
    with sessions() as session:
        accepted = [session.scalar(select(Operation.id).where(Operation.instance_id == instance_id))
                    for instance_id in instance_ids]
    statements = []

    def count_query(*args):
        statements.append(1)

    event.listen(engine, 'before_cursor_execute', count_query)
    try:
        response = client.get('/v1/operations?limit=50', headers=tenant_a)
    finally:
        event.remove(engine, 'before_cursor_execute', count_query)
    assert response.status_code == 200
    assert {row['operation_id'] for row in response.json()} == set(accepted)
    # La proyección de fulfillment necesita una consulta adicional compartida
    # para la página; el límite sigue siendo constante y no depende de las 12
    # operaciones devueltas.
    assert len(statements) <= 10, 'List work grew per operation instead of per page'
    assert response.json() == [client.get('/v1/operations/' + identifier, headers=tenant_a).json()
                               for identifier in accepted]
    page = client.get('/v1/operations?limit=3&offset=2', headers=tenant_a).json()
    assert page == response.json()[2:5]
    assert client.get('/v1/operations?status=queued', headers=tenant_a).json() == []


def test_batch_list_matches_detail_for_all_related_records_and_uncertain_creation(
        client, auth, sessions):
    identifier = managed_instance(sessions, BOOTSTRAP_TENANT_ID, vmid=30001)
    with sessions() as session:
        creation = session.scalar(select(Operation).where(Operation.instance_id == identifier))
        approval = session.scalar(select(TerraformApply).where(TerraformApply.instance_id == identifier))
        creation.status, creation.error_code = 'failed', 'PROXMOX_OBSERVATION_FAILED'
        approval.status = 'uncertain'
        operations = [creation]
        for kind in ['power.start', 'delete', 'network.discover', 'network.initialize']:
            row = Operation(id=str(uuid4()), tenant_id=BOOTSTRAP_TENANT_ID, instance_id=identifier,
                            kind='instance.' + kind, status='completed', idempotency_key=kind,
                            request_hash='a' * 64, requested_by='bootstrap')
            session.add(row)
            operations.append(row)
        session.flush()
        session.add_all([
            PowerExecution(operation_id=operations[1].id, instance_id=identifier, action='start',
                           status='completed', phase='finished', vmid=30001, node='mng01',
                           name='fixture-kvm', approval_id=approval.id, ownership_revision='a' * 40,
                           result={'power_state': 'running'}),
            DeletionExecution(operation_id=operations[2].id, instance_id=identifier,
                              status='completed', phase='deleted', vmid=30001, node='mng01',
                              name='fixture-kvm', provisioning_binding='b' * 64),
            NetworkDiscovery(operation_id=operations[3].id, instance_id=identifier,
                             provisioning_binding='b' * 64, result={'status': 'pending'}),
            GuestInitialization(operation_id=operations[4].id, instance_id=identifier,
                                provisioning_binding='b' * 64, mode='fixture', program_sha256='c' * 64,
                                mac_address='00:11:22:33:44:55', status='completed', pid=42),
        ])
        for row in operations:
            for number in range(20):
                session.add(OperationEvent(operation_id=row.id, status=row.status,
                                           code=f'fixture-{number}', actor='fixture'))
        session.commit()
    rows = client.get('/v1/operations?instance_id=' + identifier, headers=auth).json()
    assert len(rows) == 5
    for row in rows:
        assert len(row['events']) == 20
        assert row == client.get('/v1/operations/' + row['operation_id'], headers=auth).json()
    assert rows[0]['normalized_status'] == 'uncertain'
    assert rows[0]['recovery_required'] is True
