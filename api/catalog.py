"""Catálogo técnico único para planes y Golden Images de MNG."""

import json
import os
from pathlib import Path


def catalog_path() -> Path:
    return Path(os.getenv("IACP_MNG_CATALOG", "/app/terraform/environments/mng01/catalog.json"))


def load_catalog() -> dict:
    with catalog_path().open(encoding="utf-8") as handle:
        return json.load(handle)


def plans() -> list[dict]:
    return [dict(plan) for plan in load_catalog().get("plans", [])]


def resolve_plan(plan_id: str) -> dict:
    for plan in plans():
        if plan["plan_id"] == plan_id:
            return plan
    raise ValueError("PLAN_NOT_FOUND")


def images() -> list[dict]:
    return [dict(image) for image in load_catalog().get("images", [])]
