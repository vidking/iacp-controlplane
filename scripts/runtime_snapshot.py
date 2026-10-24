"""Read-only inspection, fed to Python in the existing API container over stdin.

Works on schemas 0004-0006; only digests/counts leave the database.
"""
import hashlib
import json

from redis import Redis
from sqlalchemy import MetaData, select, text

from iacp.config import broker_url
from iacp.db import get_engine

CRM_INSTANCE_FIELDS = {'commercial_context', 'start_on_create', 'initial_access_ciphertext', 'initial_access_fingerprint', 'access_verification'}


def snapshot():
    report = {"tables": {}, "nonterminal": {}, "compatibility": {}}
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
            added_crm = CRM_INSTANCE_FIELDS if name == 'instances' else {'correlation_id'} if name == 'operations' else set()
            if added_crm and added_crm <= set(table.c.keys()):
                projected = sorted(hashlib.sha256(json.dumps({k:v for k,v in dict(row).items() if k not in added_crm},
                    default=str,sort_keys=True,separators=(',',':')).encode()).hexdigest() for row in rows)
                report['compatibility'][name+'_before_0011']={'count':len(rows),
                    'sha256':hashlib.sha256(''.join(projected).encode()).hexdigest(),
                    'new_fields_default':all(all(row[k] is (False if k == 'start_on_create' else None) for k in added_crm) for row in rows)}
            if name=='instances' and 'network_profile' in table.c:
                added={'network_profile','network_mode','ip_addresses','network_observation'}
                added.update({'node_preference','self_service_policy_id','policy_binding_sha256'})
                added.update(CRM_INSTANCE_FIELDS)
                projected=sorted(hashlib.sha256(json.dumps({k:v for k,v in dict(row).items() if k not in added},
                    default=str,sort_keys=True,separators=(',',':')).encode()).hexdigest() for row in rows)
                report['compatibility']['instances_before_0008']={
                    'count':len(rows),'sha256':hashlib.sha256(''.join(projected).encode()).hexdigest(),
                    'new_fields_default':all(row['network_profile'] is None and row['network_mode']=='static'
                        and row['ip_addresses']==[] and row['network_observation'] is None for row in rows)}
            if name in {'instances','deletion_executions'} and ('self_service_policy_id' in table.c or 'autonomous' in table.c):
                added=({'node_preference','self_service_policy_id','policy_binding_sha256'} | CRM_INSTANCE_FIELDS if name=='instances' else {'autonomous'})
                projected=sorted(hashlib.sha256(json.dumps({k:v for k,v in dict(row).items() if k not in added},
                    default=str,sort_keys=True,separators=(',',':')).encode()).hexdigest() for row in rows)
                report['compatibility'][name+'_before_0009']={'count':len(rows),
                    'sha256':hashlib.sha256(''.join(projected).encode()).hexdigest(),
                    'new_fields_default':all(all(row[k] is (False if k == 'start_on_create' else None) for k in added if k in row) if name=='instances'
                        else row['autonomous'] is False for row in rows)}
            if name in {"operations", "terraform_applies", "power_executions", "deletion_executions", "guest_initializations", "jobs"}:
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
