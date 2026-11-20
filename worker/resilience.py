"""Contención de errores transitorios en los bucles de los workers.

Un fallo pasajero de PostgreSQL en el claim, la recuperación o el heartbeat
en reposo terminaba el proceso. Docker lo reiniciaba, pero se perdía el
contexto y se generaban reinicios en cascada mientras la base no respondía.
Los errores de una operación siguen gestionándose en su propio flujo: una
operación que pierde su heartbeat se recupera como ``uncertain``.
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Callable


class Backoff:
    def __init__(self, base: float, maximum: float = 30.0):
        self.base = max(0.1, base)
        self.maximum = max(self.base, maximum)
        self.failures = 0

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> float:
        self.failures += 1
        return min(self.maximum, self.base * (2 ** min(self.failures, 8)))


def report(worker_id: str, stage: str, error: BaseException, delay: float) -> None:
    summary = "".join(traceback.format_exception_only(type(error), error)).strip()
    # Solo la última línea: el mensaje de psycopg puede incluir la consulta,
    # nunca parámetros; se recorta para no inundar los logs.
    print(
        f"[{worker_id}] error transitorio en {stage}: {summary.splitlines()[-1][:300]}; "
        f"reintento en {delay:.1f}s",
        file=sys.stderr,
        flush=True,
    )


def retry_until_ready(worker_id: str, stage: str, action: Callable[[], object], backoff: Backoff) -> None:
    """Reintenta una acción de arranque (esquema, heartbeat inicial)."""

    while True:
        try:
            action()
            backoff.success()
            return
        except Exception as error:  # noqa: BLE001 - se informa y se reintenta
            delay = backoff.failure()
            report(worker_id, stage, error, delay)
            time.sleep(delay)
