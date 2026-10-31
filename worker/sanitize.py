"""Redacción mínima para detalles técnicos persistidos."""

import re


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(password|passwd|token|secret|authorization|access[_-]?key|secret[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def sanitize_text(value: str | None, *, limit: int = 100_000) -> str:
    if not value:
        return ""
    redacted = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", str(value))
    return redacted[-limit:]
