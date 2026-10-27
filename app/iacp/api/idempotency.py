"""Resolución uniforme de intenciones HTTP para mutaciones de IACP."""

from uuid import uuid4

from fastapi import Response


KEY_PATTERN = r"^[A-Za-z0-9._:-]+$"


def generated_key(operation: str) -> str:
    """Genera una intención opaca y válida cuando el cliente no la envía."""
    return f"http-{operation}-{uuid4().hex}"


def resolve_key(key: str | None, operation: str, response: Response) -> str:
    """Conserva una clave explícita o crea una nueva y la devuelve en la respuesta."""
    resolved = key or generated_key(operation)
    response.headers["Idempotency-Key"] = resolved
    return resolved

