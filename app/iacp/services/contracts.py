"""Clasificación del contrato de creación sin decisiones de negocio."""
CORE_CONTRACT = "provisioning-api-core"


def creation_contract() -> str:
    # El CRM conserva sus referencias comerciales, pero IACP solo expone un
    # contrato técnico operativo. No existe un selector de motor histórico.
    return CORE_CONTRACT


def is_core_contract() -> bool:
    return True


def is_managed_creation_contract() -> bool:
    """Todas las nuevas admisiones usan la admisión KVM Core."""
    return True


def instance_uses_managed_contract(instance) -> bool:
    # La revisión histórica no selecciona un motor. La pertenencia técnica
    # se valida en ``technical_ownership`` para cualquier instancia viva.
    return getattr(instance, "status", None) != "deleted"
