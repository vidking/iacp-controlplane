#!/usr/bin/env python3
"""Verificar enlaces locales y el contrato usado por los manuales sin acceder a red."""
import ast
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    documents = sorted((ROOT / "docs" / "handbook").glob("*.md"))
    if not documents:
        raise ValueError("Faltan los manuales")
    for document in documents:
        for link in re.findall(r"\[[^\]]*\]\(([^)#]+)(?:#[^)]*)?\)", document.read_text()):
            if "://" in link:
                continue
            if not (document.parent / link).resolve().is_file():
                raise ValueError(f"Enlace local sin destino: {document.name}: {link}")
        for language, body in re.findall(r"```(sh|python)\n(.*?)```", document.read_text(), re.S):
            if language == "python":
                ast.parse(body)
            else:
                subprocess.run(["bash", "-n"], input=body, text=True, check=True)
    api = json.loads((ROOT / "docs" / "openapi.json").read_text())
    if api["info"]["version"] != "0.6.0" or api["servers"][0]["url"] != "https://iac.cyberfuel.com":
        raise ValueError("Versión/servidor del contrato distintos de los documentados")
    required = [
        ("/v1/instances", "post"), ("/v1/instances", "get"), ("/v1/instances/{instance_id}", "get"),
        ("/v1/jobs/{job_id}", "get"), ("/v1/operations/{operation_id}", "get"),
        ("/v1/instances/{instance_id}/terraform/approvals", "post"),
        ("/v1/instances/{instance_id}/terraform/apply", "post"),
        ("/v1/instances/{instance_id}/terraform/applies", "get"),
        ("/v1/instances/{instance_id}/terraform/applies/{approval_id}/recover", "post"),
        ("/v1/instances/{instance_id}/power", "get"),
        ("/v1/instances/{instance_id}/power/start", "post"),
        ("/v1/instances/{instance_id}/power/shutdown", "post"),
        ("/v1/instances/{instance_id}/power/reboot", "post"),
    ]
    for path, method in required:
        operation = api["paths"][path][method]
        if operation.get("security") != [{"HTTPBearer": []}]:
            raise ValueError(f"Contrato sin identidad de servicio: {path}")
    create = api["paths"]["/v1/instances"]["post"]
    if "202" not in create["responses"] or not any(row["name"] == "Idempotency-Key" and row["required"] for row in create["parameters"]):
        raise ValueError("Contrato de creación no idempotente/asíncrono")
    if "delete" in api["paths"]["/v1/instances/{instance_id}"]:
        raise ValueError("Actualizar la matriz: eliminación API todavía documentada como pendiente")
    print(json.dumps({"status": "passed", "network_contacted": False, "documents": len(documents),
                      "authenticated_contracts": len(required), "api_version": api["info"]["version"],
                      "api_url": api["servers"][0]["url"]}, sort_keys=True))


if __name__ == "__main__":
    main()
