"""Read-only inspection, fed to Python in the existing API container over stdin.

Works on schemas 0004-0006; only digests/counts leave the database.
"""
import hashlib
import json

from redis import Redis
from sqlalchemy import MetaData, select, text

from iacp.config import broker_url
from iacp.db import get_engine


def snapshot():
    report = {"tables": {}, "nonterminal": {}}
    with get_engine().connect() as connection:
        # One consistent DB snapshot; never repair rows or reinterpret operations.
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        metadata = MetaData()
        metadata.reflect(connection)
        report["schema"] = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
        for name, table in sorted(metadata.tables.items()):
            if name == "alembic_version":
                continue
            rows = list(connection.execute(select(table)).mappings())
            hashes = sorted(hashlib.sha256(json.dumps(dict(row), default=str, sort_keys=True,
                            separators=(",", ":")).encode()).hexdigest() for row in rows)
            report["tables"][name] = {"count": len(rows), "sha256": hashlib.sha256("".join(hashes).encode()).hexdigest()}
            if name in {"operations", "terraform_applies", "power_executions", "deletion_executions", "jobs"}:
                terminal = {"completed", "failed", "cancelled"}
                report["nonterminal"][name] = [{"id": row.get("id", row.get("operation_id")), "status": row["status"]}
                    for row in rows if row["status"] not in terminal]
    with Redis.from_url(broker_url(), socket_connect_timeout=3, socket_timeout=3) as redis:
        report["queues"] = {}
        for key in redis.scan_iter(count=100):
            name = key.decode("utf8", errors="replace")
            if name.split("\x06\x16")[0] in {"celery", "terraform", "lifecycle"}:
                report["queues"][name] = redis.llen(key)
        report["unacked"] = redis.hlen("unacked")
        report["redis"] = redis.ping()
    return report


if __name__ == "__main__":
    print(json.dumps(snapshot(), sort_keys=True))
