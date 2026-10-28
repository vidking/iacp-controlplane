"""Identidades internas de automatización técnica.

Estas identidades no representan a una persona ni a un aprobador humano. Se
persisten en la auditoría para dejar claro qué automatización consumió un plan
o un destroy después de que la identidad técnica del tenant fue autorizada.
"""

from ..security import Principal


CORE_AUTOMATION_SUBJECT = "iacp-automation:provisioning-api-core"
COMMERCIAL_AUTOMATION_SUBJECT = "iacp-automation:commercial-g"


def technical_principal(tenant_id: str, contract: str) -> Principal:
    """Devuelve la identidad explícita que consume una ejecución delegada."""
    subjects = {
        "provisioning-api-core": CORE_AUTOMATION_SUBJECT,
        "commercial-g": COMMERCIAL_AUTOMATION_SUBJECT,
    }
    try:
        subject = subjects[contract]
    except KeyError:
        raise ValueError("Contrato sin identidad de automatización") from None
    return Principal(tenant_id, subject, "automation", True)
