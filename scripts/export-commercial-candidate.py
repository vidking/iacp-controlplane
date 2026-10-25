#!/usr/bin/env python3
"""Exportar la revisión G para migración; no cambia el contrato servido."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from iacp.commercial_contract import CommercialInstanceCreate, EXAMPLES
from iacp.main import create_app


def candidate_contract():
    previous=os.environ.get('IACP_CREATION_CONTRACT')
    try:
        os.environ['IACP_CREATION_CONTRACT']='commercial-g'
        contract = deepcopy(create_app().openapi())
    finally:
        if previous is None:os.environ.pop('IACP_CREATION_CONTRACT',None)
        else:os.environ['IACP_CREATION_CONTRACT']=previous
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
        "Redis AES-256-GCM efímero y bootstrap SSH sin contraseña en plan/state. NO DESPLEGADA: falta E2E y capacidad verificable.")
    contract["x-iacp-creation-revision"] = "fase-g-candidata"
    contract["x-iacp-deployment-status"] = "NO_GO_E2E_Y_CAPACIDAD"
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
