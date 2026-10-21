#!/usr/bin/env python3
"""Run from the repository root with PYTHONPATH=app. No runtime secrets needed."""
import json
import argparse
from pathlib import Path

from iacp.main import create_app

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--stdout", action="store_true", help="Exporta JSON para redirección desde Docker")
args = parser.parse_args()
serialized = json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
if args.stdout:
    print(serialized, end="")
else:
    target = Path(__file__).resolve().parents[1] / "docs/openapi.json"
    target.write_text(serialized)
    print(target)
