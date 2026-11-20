"""Healthcheck de workers para Docker Compose.

Uso: ``python -m worker.healthcheck worker-3`` o
``python -m worker.healthcheck service-worker-1``.

Un worker está sano si su último heartbeat en PostgreSQL es reciente. El
worker ocupado sigue enviando heartbeat mientras corre Terraform o espera a
Proxmox, así que «ocupado» no se confunde con «bloqueado». Es una sola
lectura: no ejecuta DDL, no escribe y no toma locks. Docker no reinicia un
contenedor ``unhealthy``; el estado sirve para supervisión y para el gate de
despliegue.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _max_age() -> float:
    try:
        return max(30.0, float(os.getenv("IACP_WORKER_HEALTH_MAX_AGE_SECONDS", "300")))
    except ValueError:
        return 300.0


def _table(worker_id: str) -> str:
    return "service_workers" if worker_id.startswith("service-worker-") else "workers"


def check(worker_id: str) -> tuple[bool, str]:
    from api.db import connect

    try:
        connection = connect()
        try:
            row = connection.execute(
                f"SELECT status, last_heartbeat FROM {_table(worker_id)} WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
        finally:
            connection.close()
    except Exception as error:  # noqa: BLE001 - cualquier fallo es «no sano»
        return False, f"base de datos no disponible: {type(error).__name__}"
    if row is None:
        return False, "worker sin registrar"
    if row["status"] == "stopped":
        return False, "worker detenido"
    try:
        last = datetime.fromisoformat(row["last_heartbeat"])
    except (TypeError, ValueError):
        return False, "heartbeat inválido"
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age > _max_age():
        return False, f"heartbeat obsoleto ({age:.0f}s)"
    return True, f"{row['status']} ({age:.0f}s)"


def detect_worker_id(proc: Path = Path("/proc")) -> str | None:
    """Obtiene ``--worker-id`` del proceso worker del mismo contenedor.

    Permite declarar un único healthcheck en el ancla común de Compose.
    """

    for cmdline in sorted(proc.glob("[0-9]*/cmdline")):
        try:
            parts = cmdline.read_bytes().split(b"\0")
        except OSError:
            continue
        if b"--worker-id" in parts:
            index = parts.index(b"--worker-id")
            if index + 1 < len(parts) and parts[index + 1]:
                return parts[index + 1].decode("utf-8", "replace")
    return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        detected = detect_worker_id()
        args = [detected] if detected else []
    if len(args) != 1:
        print("uso: python -m worker.healthcheck [worker-id]", file=sys.stderr)
        return 2
    os.environ.setdefault("IACP_DB_CONNECT_TIMEOUT", "3")
    healthy, detail = check(args[0])
    print(detail)
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
