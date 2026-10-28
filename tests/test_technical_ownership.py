"""Regresiones del vínculo técnico único, sin LabPolicy operativa."""

import pytest
from sqlalchemy import select

from iacp.models import Instance, TechnicalOwnership, TerraformApply, TerraformExecution
from iacp.services.instances import Conflict
from iacp.services.technical_ownership import member
from iacp.services.technical_ownership_migration import migrate
from tests.power_helpers import managed_instance
from tests.test_instances import identities


def test_historical_source_revision_is_adopted_without_policy_or_release_pin(sessions):
    identifier = managed_instance(sessions, "00000000-0000-0000-0000-000000000001")
    with sessions() as session:
        instance = session.get(Instance, identifier)
        instance.commercial_revision = "commercial-g"
        execution = session.get(TerraformExecution, identifier)
        execution.source_revision = "a" * 40
        ownership = member(session, instance, "start", adopt=True)
        session.commit()
        row = session.get(TechnicalOwnership, identifier)
        assert ownership.binding_sha256 == row.binding_sha256
        assert row.source_revision == "a" * 40
        assert row.state_key == execution.state_key


def test_repeated_adoption_is_idempotent_and_keeps_one_binding(sessions):
    identifier = managed_instance(sessions, "00000000-0000-0000-0000-000000000001")
    with sessions() as session:
        instance = session.get(Instance, identifier)
        first = member(session, instance, "start", adopt=True)
        second = member(session, instance, "delete", adopt=True)
        session.commit()
        assert first.binding_sha256 == second.binding_sha256
        assert session.query(TechnicalOwnership).count() == 1


def test_changed_state_or_vmid_fails_closed_without_rebinding(sessions):
    identifier = managed_instance(sessions, "00000000-0000-0000-0000-000000000001")
    with sessions() as session:
        instance = session.get(Instance, identifier)
        member(session, instance, "start", adopt=True)
        session.commit()
        instance = session.get(Instance, identifier)
        execution = session.get(TerraformExecution, identifier)
        execution.state_key = execution.state_key + ".changed"
        changed_key = execution.state_key
        with pytest.raises(Conflict, match="TECHNICAL_OWNERSHIP_BINDING_CHANGED"):
            member(session, instance, "start", adopt=True)
        session.rollback()
        assert session.get(TechnicalOwnership, identifier).state_key != changed_key


def test_protected_vmid_cannot_be_adopted(sessions):
    identifier = managed_instance(sessions, "00000000-0000-0000-0000-000000000001")
    with sessions() as session:
        instance = session.get(Instance, identifier)
        execution = session.get(TerraformExecution, identifier)
        apply = session.scalar(select(TerraformApply).where(TerraformApply.instance_id == execution.instance_id))
        instance.vmid = execution.vmid = 104
        apply.result = {**apply.result, "vmid": 104}
        with pytest.raises(Conflict, match="TECHNICAL_OWNERSHIP_EVIDENCE_REQUIRED"):
            member(session, instance, "start", adopt=True)


def test_migration_is_repeatable_and_preserves_deleted_history(sessions):
    live = managed_instance(sessions, "00000000-0000-0000-0000-000000000001", vmid=30011)
    deleted = managed_instance(sessions, "00000000-0000-0000-0000-000000000001", vmid=30012,
                               name="deleted-kvm")
    with sessions() as session:
        session.get(Instance, deleted).status = "deleted"
        session.commit()
        first = migrate(session)
        second = migrate(session)
        assert first["adopted"] == 1
        assert first["deleted"] == 1
        assert second["adopted"] == 0
        assert second["already_bound"] == 1
        assert session.get(TechnicalOwnership, live) is not None
        assert session.get(TechnicalOwnership, deleted) is None


def test_other_tenant_cannot_use_the_same_instance_from_api(client, identities, sessions):
    identifier = managed_instance(sessions, identities["tenant-a-operator"]["tenant_id"], vmid=30013,
                                  name="tenant-owned")
    response = client.get(f"/v1/instances/{identifier}", headers=identities["tenant-b-reader"]["headers"])
    assert response.status_code == 404
