#!/usr/bin/env python3
"""Run from the repository root with PYTHONPATH=app. No runtime secrets needed."""
import json
from pathlib import Path

from iacp.main import create_app

target = Path(__file__).resolve().parents[1] / "docs/openapi.json"
target.write_text(json.dumps(create_app().openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print(target)
