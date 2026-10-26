"""Clasificación del contrato de creación sin decisiones de negocio."""
import os


CORE_CONTRACT = "provisioning-api-core"
COMMERCIAL_CONTRACT = "commercial-g"


def creation_contract() -> str:
    return os.environ.get("IACP_CREATION_CONTRACT", "historical")


def is_core_contract() -> bool:
    return creation_contract() == CORE_CONTRACT


def is_managed_creation_contract() -> bool:
    """Contratos que usan la admisión KVM del catálogo y Terraform real."""
    return creation_contract() in {COMMERCIAL_CONTRACT, CORE_CONTRACT}


def instance_uses_managed_contract(instance) -> bool:
    return getattr(instance, "commercial_revision", None) in {
        COMMERCIAL_CONTRACT, CORE_CONTRACT
    }
