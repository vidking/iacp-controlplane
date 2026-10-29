#!/usr/bin/env python3
"""Adaptador del cierre histórico previo a SSH de un bootstrap comercial."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from iacp.services.commercial_bootstrap_recovery import (  # noqa: E402
    COMMERCIAL_BOOTSTRAP_SHA256,
    ERROR_CODE,
    EVENT_CODE,
    HISTORICAL_RUNTIME_SHA,
    MARKER_PATH,
    PRECONNECT_SEQUENCE,
    RecoveryError,
    SECURE_BOOTSTRAP_SHA256,
    assert_broker_and_workers_idle,
    recover_in_session,
    run_from_json,
    validate_evidence,
    validate_request,
)

__all__ = [
    "COMMERCIAL_BOOTSTRAP_SHA256",
    "ERROR_CODE",
    "EVENT_CODE",
    "HISTORICAL_RUNTIME_SHA",
    "MARKER_PATH",
    "PRECONNECT_SEQUENCE",
    "RecoveryError",
    "SECURE_BOOTSTRAP_SHA256",
    "assert_broker_and_workers_idle",
    "recover_in_session",
    "run_from_json",
    "validate_evidence",
    "validate_request",
]
