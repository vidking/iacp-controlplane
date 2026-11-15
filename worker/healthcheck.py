"""Healthcheck de un worker para Docker Compose.

Uso: ``python -m worker.healthcheck worker-3``

El worker está sano si su último heartbeat en PostgreSQL es reciente. Un
worker ocupado sigue enviando heartbeat cada pocos segundos mientras corre
Terraform o espera a Proxmox, así que "ocupado" no se confunde con
"bloqueado". No ejecuta migraciones ni escrituras: es una sola lectura.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def _max_age() -> float:
    try:
        return max(10.0, float(os.getenv("IACP_WORKER_HEALTH_MAX_AGE_SECONDS", "120")))
    except ValueError:
        return 120.0


def check(worker_id: str) -> tuple[bool, str]:
    from api.db import database

    try:
        with database() as db:
            row = db.execute(
                "SELECT status, last_heartbeat FROM workers WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
    except Exception as error:  # noqa: BLE001 - cualquier fallo es "no sano"
        return False, f"database unavailable: {type(error).__name__}"
    if row is None:
        return False, "worker not registered"
    if row["status"] == "stopped":
        return False, "worker stopped"
    try:
        last = datetime.fromisoformat(row["last_heartbeat"])
    except (TypeError, ValueError):
        return False, "invalid heartbeat"
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age > _max_age():
        return False, f"heartbeat stale ({age:.0f}s)"
    return True, f"{row['status']} ({age:.0f}s)"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("uso: python -m worker.healthcheck <worker-id>", file=sys.stderr)
        return 2
    # Proceso efímero: una conexión de corta vida, identificada en pg_stat_activity.
    os.environ.setdefault("IACP_DB_APPLICATION_NAME", f"iacp-healthcheck-{args[0]}")
    os.environ.setdefault("IACP_DB_REUSE_CONNECTIONS", "0")
    os.environ.setdefault("IACP_DB_CONNECT_TIMEOUT", "3")
    healthy, detail = check(args[0])
    print(detail)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
