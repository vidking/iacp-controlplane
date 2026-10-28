"""Compile-time ownership handoff, promoted only with a reviewed IaC revision."""
import os

from ..terraform import config
from .proxmox import PowerError

# Pinned hardware-fidelity source (iac-proxmox), preserving the lifecycle
# ownership contract while removing provider disk defaults.
# Runtime configuration cannot override the source contract.
KVM_POWER_SOURCE_REVISION: str | None = "b517c9bb67a9e3c800f0c3657c56224c2b8f80aa"
KVM_POWER_SOURCE_ARCHIVE_SHA256 = "68ddafc66df5a96cd3e22165658a273446d9d0f9fb46115f2e065034c2ddf2a3"
# Las revisiones previas permanecen observables para reconciliar operaciones ya
# certificadas; nunca se utilizan para crear un plan nuevo.
PREVIOUS_POWER_SOURCE_REVISIONS = frozenset({
    "e701a0592d1caeda76faf7bdaa1e04c0b9bae5fc",
    "9aeb8ec846342d279a5d16be7c8e20157e9bf5ae",
})
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
    """Validate one technical ownership record for any source revision.

    ``mutation_revision`` still protects the running Lifecycle provider, but
    the revision that created this VM is immutable evidence and is not compared
    with the current release pin.
    """
    if ownership_revision() is None:
        raise PowerError("POWER_OWNERSHIP_UNAVAILABLE")
    from ..services.authorization import member
    from ..services.instances import Conflict

    try:
        member(session, instance, "start")
    except Conflict as failure:
        raise PowerError(str(failure)) from None
    # PowerExecution retains the nullable legacy certificate column for audit;
    # the unified record is checked above and does not fabricate a certificate.
    return None
