#!/usr/bin/env python3
"""Verificar enlaces locales y el contrato usado por los manuales sin acceder a red."""
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


def main():
    documents = sorted((ROOT / "docs" / "handbook").glob("*.md"))
    documents.extend(ROOT / "docs" / name for name in ["OPERATIONS.md","SAFE_DELETE.md","DHCP_PILOT.md","DHCP_RECOVERY.md","SELF_SERVICE.md","SELF_SERVICE_VALIDATION.md","CRM_INTEGRATION.md","ENGINE_INVENTORY.md"])
    documents.extend(ROOT / 'docs' / name for name in ['MVP_CRM_RUNBOOK.md','MVP_CHECKLIST.md','IACOC_CONTRACT.md','STATE_RETENTION_AUDIT.md'])
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
    if api["info"]["version"] != "0.10.1" or api["servers"][0]["url"] != "https://iac.cyberfuel.com":
        raise ValueError("Versión/servidor del contrato distintos de los documentados")
    administrative = json.loads((ROOT / "docs/openapi-admin.json").read_text())
    required = [
        ('/v1/events', 'get'), ('/v1/observability', 'get'),
        ('/v1/plans', 'get'), ('/v1/images', 'get'),
        ('/v1/instances', 'post'), ('/v1/instances', 'get'),
        ('/v1/instances/{instance_id}', 'get'), ('/v1/instances/{instance_id}', 'delete'),
        ('/v1/instances/{instance_id}/access/verify', 'post'),
        ('/v1/instances/{instance_id}/network/discover', 'post'),
        ('/v1/instances/{instance_id}/network/initialize', 'post'),
        ('/v1/instances/{instance_id}/power', 'get'),
        ('/v1/instances/{instance_id}/power/start', 'post'),
        ('/v1/instances/{instance_id}/power/shutdown', 'post'),
        ('/v1/instances/{instance_id}/power/reboot', 'post'),
        ('/v1/jobs/{job_id}', 'get'), ('/v1/operations', 'get'),
        ('/v1/operations/{operation_id}', 'get'),
        ('/v1/instances/{instance_id}/provisioning/plan', 'get'),
        ('/v1/instances/{instance_id}/provisioning/plan/cancel', 'post'),
        ('/v1/instances/{instance_id}/provisioning/plan/retry', 'post'),
        ('/v1/instances/{instance_id}/provisioning/approvals', 'post'),
        ('/v1/instances/{instance_id}/provisioning/executions', 'get'),
        ('/v1/instances/{instance_id}/provisioning/executions', 'post'),
        ('/v1/instances/{instance_id}/provisioning/deletion-plan', 'post'),
        ('/v1/instances/{instance_id}/provisioning/deletion-approval', 'post'),
    ]
    for path, method in required:
        source = administrative if "/provisioning/" in path else api
        if path not in source["paths"] or method not in source["paths"][path]:
            raise ValueError(f"Ruta del contrato ausente: {method.upper()} {path}")
        operation = source["paths"][path][method]
        if operation.get("security") != [{"HTTPBearer": []}]:
            raise ValueError(f"Contrato sin identidad de servicio: {path}")
    create = api["paths"]["/v1/instances"]["post"]
    from iacp.commercial_contract import CommercialInstanceCreate
    for example in (ROOT/'docs/examples/crm').glob('*.json'):
        CommercialInstanceCreate.model_validate_json(example.read_text())
    if "202" not in create["responses"] or not any(row["name"] == "Idempotency-Key" for row in create["parameters"]):
        raise ValueError("Contrato de creación no idempotente/asíncrono")
    if any(path.startswith("/v1/lab/") for path in api["paths"] | administrative["paths"]):
        raise ValueError("El contrato activo todavía publica rutas LabPolicy")
    print(json.dumps({"status": "passed", "network_contacted": False, "documents": len(documents),
                      "authenticated_contracts": len(required), "api_version": api["info"]["version"],
                      "api_url": api["servers"][0]["url"]}, sort_keys=True))


if __name__ == "__main__":
    main()
