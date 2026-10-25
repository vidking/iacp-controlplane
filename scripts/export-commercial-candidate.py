#!/usr/bin/env python3
"""Exportar la revisión G para migración; no cambia el contrato servido."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from iacp.commercial_contract import CommercialInstanceCreate, EXAMPLES
from iacp.main import create_app


def candidate_contract():
    contract = deepcopy(create_app().openapi())
    schema = CommercialInstanceCreate.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = schema.pop("$defs", {})
    contract["components"]["schemas"].update(definitions)
    contract["components"]["schemas"]["CommercialInstanceCreate"] = schema
    operation = contract["paths"]["/v1/instances"]["post"]
    operation["requestBody"]["content"]["application/json"] = {
        "schema": {"$ref": "#/components/schemas/CommercialInstanceCreate"},
        "examples": {f"kvm_{n}": {"summary": f"KVM {n}; contraseña ilustrativa, ejecución bloqueada G6",
            "value": example} for n, example in enumerate(EXAMPLES, 1)}}
    operation["description"] = ("Revisión candidata incompatible de creación: plan_id, image_id y network.profile_id. "
        "Sin tamaños libres ni UUID de política. Idempotency-Key obligatorio; 202 conserva identificadores. "
        "NO SERVIDA: requiere entrega root fuera de PostgreSQL/plan/state, admisión interna y consumidores migrados.")
    contract["x-iacp-creation-revision"] = "fase-g-candidata"
    contract["x-iacp-deployment-status"] = "BLOCKED_G6_Y_MIGRACION"
    return contract


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true")
    args = parser.parse_args()
    text = json.dumps(candidate_contract(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.stdout:
        print(text, end="")
    else:
        target = ROOT / "docs/contracts/openapi-fase-g-candidato.json"
        target.write_text(text)
        print(target)
