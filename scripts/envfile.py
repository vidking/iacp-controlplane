"""Lectura limitada de archivos dotenv sin dependencias de terceros."""

from __future__ import annotations

import ast
import re
from pathlib import Path


_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_BARE_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:#.*)?$")


def read_env_file(path: Path) -> dict[str, str | None]:
    """Lee asignaciones dotenv sin interpolar variables ni registrar sus valores."""

    result: dict[str, str | None] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.match(line)
        if not match:
            bare_key = _BARE_KEY.match(line)
            if bare_key:
                result[bare_key.group(1)] = None
                continue
            raise ValueError(f"Asignación dotenv inválida en línea {number}")
        key, raw = match.groups()
        if not raw:
            result[key] = ""
            continue
        if raw[0] in {"'", '"'}:
            quote = raw[0]
            escaped = False
            end = None
            for index, char in enumerate(raw[1:], 1):
                if quote == '"' and char == "\\" and not escaped:
                    escaped = True
                    continue
                if char == quote and not escaped:
                    end = index
                    break
                escaped = False
            if end is None:
                raise ValueError(f"Valor dotenv inválido en línea {number}")
            trailing = raw[end + 1 :].strip()
            if trailing and not trailing.startswith("#"):
                raise ValueError(f"Valor dotenv inválido en línea {number}")
            value = raw[: end + 1]
            if quote == '"':
                try:
                    parsed = ast.literal_eval(value)
                except (SyntaxError, ValueError):
                    raise ValueError(f"Valor dotenv inválido en línea {number}") from None
                if not isinstance(parsed, str):
                    raise ValueError(f"Valor dotenv inválido en línea {number}")
                result[key] = parsed
            else:
                result[key] = value[1:-1].replace("\\'", "'").replace("\\\\", "\\")
            continue
        value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
        result[key] = value
    return result
