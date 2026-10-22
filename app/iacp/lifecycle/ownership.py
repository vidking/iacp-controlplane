"""Compile-time ownership handoff, promoted only with a reviewed IaC revision."""
import os

from ..terraform import config
from .proxmox import PowerError

# Intentionally unavailable: SOURCE_REVISION still manages started. After the
# independent IaC proposal is merged, a reviewed Control Plane promotion must
# pin SOURCE_REVISION and this contract to that exact immutable commit together.
# Environment flags and API parameters cannot override this gate.
KVM_POWER_SOURCE_REVISION: str | None = None
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
