"""Redis real descartable: TTL, ciphertext y entrega fuera de la cola."""
import os
from uuid import uuid4
import pytest
from redis import Redis

from iacp.services.bootstrap_secrets import EphemeralSecrets,BootstrapSecretError

pytestmark=pytest.mark.integration


def test_real_redis_stores_only_authenticated_ciphertext_with_atomic_ttl(runtime):
    from sqlalchemy.engine import make_url
    url=os.environ['IACP_INTEGRATION_REDIS_URL']
    assert make_url(url).database=='15'
    redis=Redis.from_url(url)
    store=EphemeralSecrets(redis,os.urandom(32),120)
    canary='SENUELO-G6-REDIS-'+uuid4().hex
    ref=store.put('tenant-a','instance-a',canary);key='iacp:bootstrap:v1:'+ref
    try:
        assert 0<redis.ttl(key)<=120
        assert canary.encode() not in redis.get(key)
        assert store.read(ref,'tenant-a','instance-a')==canary
        with pytest.raises(BootstrapSecretError):store.read(ref,'tenant-b','instance-a')
        redis.expire(key,0)
        with pytest.raises(BootstrapSecretError,match='EXPIRED_OR_MISSING'):store.read(ref,'tenant-a','instance-a')
        store.restore(ref,'tenant-a','instance-a',canary)
        assert store.read(ref,'tenant-a','instance-a')==canary
        store.delete(ref);assert not redis.exists(key)
    finally:redis.delete(key);redis.close()


def test_migration_0012_preserves_0011_rows_and_historical_sensitive_ciphertext(pg_engine,runtime):
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text
    from tests.helpers import upgrade
    from iacp.models import BOOTSTRAP_TENANT_ID
    root=Path(__file__).resolve().parents[2]
    spec=importlib.util.spec_from_file_location('snapshot_commercial',root/'scripts/runtime_snapshot.py')
    snapshot=importlib.util.module_from_spec(spec);spec.loader.exec_module(snapshot);snapshot.get_engine=lambda:pg_engine
    spec=importlib.util.spec_from_file_location('promotion_commercial',root/'scripts/go_live.py')
    promotion=importlib.util.module_from_spec(spec);spec.loader.exec_module(promotion)
    upgrade(pg_engine,'0011_crm_contract')
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO instances (id,tenant_id,name,image,vcpus,memory_mb,disk_gb,status,executor,created_at,updated_at,initial_access_ciphertext) "
            "VALUES ('historical-g',:tenant,'historical-g','ubuntu-2404',2,2048,20,'ready','terraform',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'fixture-historical-ciphertext')"),{'tenant':BOOTSTRAP_TENANT_ID})
    before=snapshot.snapshot();upgrade(pg_engine);after=snapshot.snapshot()
    promotion.compare_history(before,after)
    assert after['schema']==['0015_technical_access']
    assert after['tables']['commercial_intents']['count']==0
    with pg_engine.connect() as c:
        assert c.execute(text('SELECT commercial_revision,initial_access_ciphertext FROM instances')).one()==(None,'fixture-historical-ciphertext')
