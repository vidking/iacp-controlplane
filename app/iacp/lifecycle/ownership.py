"""Compile-time ownership handoff, promoted only with a reviewed IaC revision."""
import os

from ..terraform import config
from .proxmox import PowerError

# iac-proxmox PR #7, merged into main. Runtime configuration cannot override it.
KVM_POWER_SOURCE_REVISION: str | None = "9aeb8ec846342d279a5d16be7c8e20157e9bf5ae"
KVM_POWER_SOURCE_ARCHIVE_SHA256 = "98f0900d571a8d05e4bbb0eabdb10550a372655f61fda35aaa3a601537daa94a"
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
    if execution.source_revision == revision:
        return None
    if execution.source_revision != LEGACY_TERRAFORM_POWER_SOURCE:
        raise PowerError("POWER_SOURCE_REVISION_UNSUPPORTED")
    certificate = session.scalar(select(PowerOwnershipCertificate).where(
        PowerOwnershipCertificate.instance_id == instance.id,
        PowerOwnershipCertificate.approval_id == approval.id,
        PowerOwnershipCertificate.ownership_revision == revision))
    if (certificate is None or certificate.source_revision != execution.source_revision
            or certificate.binding_sha256 != binding_digest(instance, execution, approval)):
        raise PowerError("POWER_OWNERSHIP_CERTIFICATION_REQUIRED")
    return certificate.id
