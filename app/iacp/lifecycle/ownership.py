"""Compile-time ownership handoff, promoted only with a reviewed IaC revision."""
import os

from ..terraform import config
from .proxmox import PowerError

# Pinned hardware-fidelity source (iac-proxmox), preserving the lifecycle
# ownership contract while removing provider disk defaults.
# Runtime configuration cannot override the source contract.
KVM_POWER_SOURCE_REVISION: str | None = "e701a0592d1caeda76faf7bdaa1e04c0b9bae5fc"
KVM_POWER_SOURCE_ARCHIVE_SHA256 = "f5f45315d832861fd5d7f9e5a14aaf0c579bf2e476ea836f32c50af43d7dc733"
PREVIOUS_POWER_SOURCE_REVISION = "9aeb8ec846342d279a5d16be7c8e20157e9bf5ae"
LEGACY_TERRAFORM_POWER_SOURCE = "40dc5d10387cfb50384ecd3ddddfd75289fe7bc4"


def ownership_revision() -> str | None:
    if (KVM_POWER_SOURCE_REVISION and KVM_POWER_SOURCE_REVISION == config.SOURCE_REVISION
            and config.SOURCE_REVISION != LEGACY_TERRAFORM_POWER_SOURCE):
        return KVM_POWER_SOURCE_REVISION
    return None


def mutation_revision() -> str:
    revision = ownership_revision()
    if revision is None:
        raise PowerError("POWER_OWNERSHIP_UNAVAILABLE")
    if os.environ.get("IACP_KVM_LIFECYCLE_ENABLED") != "1":
        raise PowerError("POWER_LIFECYCLE_DISABLED")
    return revision


def instance_ownership(session, instance, approval):
    """A global source promotion never upgrades historical provisioning evidence."""
    from sqlalchemy import select
    from ..models import PowerOwnershipCertificate, TerraformExecution
    from .certification_binding import binding_digest

    revision = ownership_revision()
    execution = session.get(TerraformExecution, instance.id)
    if revision is None:
        raise PowerError("POWER_OWNERSHIP_UNAVAILABLE")
    scope = os.environ.get("IACP_LIFECYCLE_INSTANCE_IDS", "")
    delegated = False
    if instance.self_service_policy_id or getattr(instance,'commercial_revision',None):
        from ..services.lab import member
        from ..services.instances import Conflict
        try:
            member(session, instance, "create")
            delegated = True
        except Conflict as failure:
            raise PowerError(str(failure)) from None
    restricted = bool(scope) or os.environ.get("IACP_LAB_SELF_SERVICE_ENABLED") == "1"
    if restricted and instance.id not in scope.split(",") and not delegated:
        raise PowerError("POWER_INSTANCE_NOT_AUTHORIZED")
    if execution.source_revision in {revision, PREVIOUS_POWER_SOURCE_REVISION}:
        return None
    if execution.source_revision != LEGACY_TERRAFORM_POWER_SOURCE:
        raise PowerError("POWER_SOURCE_REVISION_UNSUPPORTED")
    certificate = session.scalar(select(PowerOwnershipCertificate).where(
        PowerOwnershipCertificate.instance_id == instance.id,
        PowerOwnershipCertificate.approval_id == approval.id,
        PowerOwnershipCertificate.ownership_revision.in_([revision, PREVIOUS_POWER_SOURCE_REVISION])))
    if (certificate is None or certificate.source_revision != execution.source_revision
            or certificate.binding_sha256 != binding_digest(instance, execution, approval)):
        raise PowerError("POWER_OWNERSHIP_CERTIFICATION_REQUIRED")
    return certificate.id
