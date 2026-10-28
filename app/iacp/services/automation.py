"""Identidades internas de automatización técnica.

Estas identidades no representan a una persona ni a un aprobador humano. Se
persisten en la auditoría para dejar claro qué automatización consumió un plan
o un destroy después de que la identidad técnica del tenant fue autorizada.
"""

from ..security import Principal


CORE_AUTOMATION_SUBJECT = "iacp-automation:provisioning-api-core"


def technical_principal(tenant_id: str, contract: str = "provisioning-api-core") -> Principal:
    """Devuelve la identidad explícita que consume una ejecución Core."""
    if contract != "provisioning-api-core":
        raise ValueError("Solo provisioning-api-core puede delegar ejecuciones")
    return Principal(tenant_id, CORE_AUTOMATION_SUBJECT, "automation", True)
