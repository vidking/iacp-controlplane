#!/usr/bin/env python3
"""Adaptador del driver para la recuperación Core de Guest Initialization.

La lógica vive en ``iacp.services.guest_recovery`` y también es la que usa la
ruta administrativa del API. El driver puede inyectar ese módulo en un API
activo anterior mientras se promociona el checkout revisado.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from iacp.services.guest_recovery import (  # noqa: E402
    ACTIVE_APPLY_STATUSES,
    ACTIVE_OPERATION_STATUSES,
    ERROR_CODE,
    EVENT_CODE,
    PROTECTED_VMIDS,
    RecoveryError,
    _load_locked_state,
    _validate_queued_state,
    recover_in_session,
    recover_queued_initialization,
    run_from_json,
    validate_request,
)

__all__ = [
    "ACTIVE_APPLY_STATUSES",
    "ACTIVE_OPERATION_STATUSES",
    "ERROR_CODE",
    "EVENT_CODE",
    "PROTECTED_VMIDS",
    "RecoveryError",
    "_load_locked_state",
    "_validate_queued_state",
    "recover_in_session",
    "recover_queued_initialization",
    "run_from_json",
    "validate_request",
]
