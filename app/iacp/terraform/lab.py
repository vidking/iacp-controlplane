"""Validación local de una asignación autorizada; no construye ejecutores ni abre red."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from ..schemas import InstanceCreate
from .config import Allocation, SOURCE_REVISION, TERRAFORM_VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allocation", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    allocation = Allocation.model_validate_json(args.allocation.read_text())
    spec = InstanceCreate.model_validate_json(args.request.read_text())
    allocation.variables(SimpleNamespace(**spec.model_dump()))
    print(json.dumps({"valid": True, "environment": allocation.environment,
                      "source_revision": SOURCE_REVISION, "terraform_version": TERRAFORM_VERSION,
                      "network_contacted": False, "apply_enabled": False}))


if __name__ == "__main__":
    main()
