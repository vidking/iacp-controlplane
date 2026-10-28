"""Regresiones del vínculo técnico único, sin LabPolicy operativa."""

import pytest
from sqlalchemy import select
from uuid import uuid4

from iacp.models import (CommercialIntent, GuestInitialization, Instance, Job, Operation,
                         TechnicalOwnership, TerraformApply, TerraformExecution)
from iacp.services.instances import Conflict
from iacp.services.guest_identity import program_for_mode
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


def _profile_snapshot(tenant_id):
    return {
        "allowed_tenants": [tenant_id],
        "settings": {
            "image": "ubuntu-2404", "environment": "lab", "allowed_nodes": ["mng01"],
            "backend": {"bucket": "fixture-iacp-lab", "region": "us-east-1",
                         "endpoint": "https://s3.wasabisys.com"},
            "template_vm_id": 29000, "node_name": "mng01", "template_node_name": "mng01",
            "pool_id": None, "clone_datastore_id": "fixture-storage",
            "network_bridge": "vmbr0", "network_mode": "dhcp", "vlan_id": None,
            "cloudinit_datastore_id": "fixture-storage", "cloudinit_username": "ubuntu",
            "ssh_public_keys": ["ssh-ed25519 AAAATESTKEYONLY"], "guest_agent_enabled": True,
            "started": False,
            "boot_disk": {"interface": "scsi0", "datastore_id": "fixture-storage",
                           "minimum_size_gb": 20},
        },
        "vmid_min": 30000, "vmid_max": 30010, "max_live_instances": 3,
        "max_vcpus": 8, "max_memory_mb": 16384, "max_disk_gb": 400,
        "target_nodes": ["mng01"], "self_service_enabled": True,
        "guest_initialization": "ubuntu-2404-lab-identity-v1", "root_password_bootstrap": False,
        "max_total_vcpus": 16, "max_total_memory_mb": 32768,
        "max_total_disk_gb": 800, "max_concurrent_operations": 2,
    }


def test_snapshot_combines_historical_profile_allocation_and_guest_evidence(sessions):
    identifier = managed_instance(sessions, "00000000-0000-0000-0000-000000000001", vmid=30014)
    mode = "ubuntu-2404-lab-identity-v1"
    program_sha = program_for_mode(mode)[1]
    with sessions() as session:
        instance = session.get(Instance, identifier)
        execution = session.get(TerraformExecution, identifier)
        instance.image = execution.allocation["image"] = "ubuntu-2404"
        instance.network_profile = "lab-dhcp"
        session.add(CommercialIntent(
            instance_id=identifier, revision="provisioning-api-core", plan_id="kvm-1",
            image_id="ubuntu-2404", catalog_sha256="a" * 64, request_fingerprint="b" * 64,
            specification={"profile_snapshot": _profile_snapshot(instance.tenant_id)},
            bootstrap_status="completed",
        ))
        operation = Operation(id=str(uuid4()), tenant_id=instance.tenant_id, instance_id=identifier,
                              kind="instance.network.initialize", status="failed",
                              idempotency_key="identity-history", request_hash="c" * 64,
                              requested_by="fixture")
        session.add(operation)
        session.add(Job(id=str(uuid4()), tenant_id=instance.tenant_id, operation_id=operation.id,
                        operation=operation.kind, status="failed"))
        session.add(GuestInitialization(
            operation_id=operation.id, instance_id=identifier, provisioning_binding="d" * 64,
            mode=mode, program_sha256=program_sha, mac_address="bc:24:11:12:34:56",
            status="failed", finished_at=None,
        ))
        technical = member(session, instance, "start", adopt=True)
        session.commit()
        snapshot = technical.profile_snapshots[instance.network_profile]
        assert snapshot["vm_id"] == execution.allocation["vm_id"]
        assert snapshot["image"] == "ubuntu-2404"
        assert snapshot["guest_initialization"] == mode
        assert snapshot["guest_initialization_program_sha256"] == program_sha
        assert snapshot["guest_initialization_evidence"]["status"] == "failed"


def test_migration_separates_failed_empty_ready_inconsistent_and_active_rows(sessions):
    adopted = managed_instance(sessions, "00000000-0000-0000-0000-000000000001", vmid=30015)
    failed = managed_instance(sessions, "00000000-0000-0000-0000-000000000001", vmid=30016,
                              name="failed-without-resource")
    ready_inconsistent = managed_instance(sessions, "00000000-0000-0000-0000-000000000001", vmid=30017,
                                          name="ready-inconsistent")
    active = managed_instance(sessions, "00000000-0000-0000-0000-000000000001", vmid=30018,
                              name="active-recovery")
    with sessions() as session:
        failed_instance = session.get(Instance, failed)
        failed_instance.status = "failed"
        failed_instance.vmid = failed_instance.node = None
        failed_execution = session.get(TerraformExecution, failed)
        failed_apply = session.scalar(select(TerraformApply).where(
            TerraformApply.instance_id == failed, TerraformApply.attempt == failed_execution.attempt))
        failed_apply.result = {"simulated": False, "observation": {
            "postconditions_verified": False, "state_present": False, "proxmox_present": False}}

        inconsistent_instance = session.get(Instance, ready_inconsistent)
        inconsistent_apply = session.scalar(select(TerraformApply).where(
            TerraformApply.instance_id == ready_inconsistent))
        inconsistent_apply.status = "failed"
        inconsistent_instance.status = "ready"

        active_creation = session.scalar(select(Operation).where(
            Operation.instance_id == active, Operation.kind == "instance.create"))
        active_creation.status = "uncertain"
        report = migrate(session)

        assert adopted not in {row["instance_id"] for row in report["blocked"]}
        assert failed in {row["instance_id"] for row in report["failed_without_resource"]}
        assert ready_inconsistent in {row["instance_id"] for row in report["reconciliation_required"]}
        assert active in {row["instance_id"] for row in report["recovery_required"]}
        assert report["blocked_count"] == 2
