"""Protecciones inmutables del Control Plane y de las VMs de laboratorio."""

from __future__ import annotations

import os


PROTECTED_VMIDS = {104, 106, 29000, 29001, 29010, 29011}


def protected_vmids() -> set[int]:
    values = set(PROTECTED_VMIDS)
    for value in os.getenv("IACP_ADDITIONAL_PROTECTED_VMIDS", "").split(","):
        try:
            parsed = int(value.strip())
        except ValueError:
            continue
        if parsed > 0:
            values.add(parsed)
    return values


def is_protected_vmid(vmid: int | str | None) -> bool:
    try:
        return int(vmid) in protected_vmids()
    except (TypeError, ValueError):
        return False
