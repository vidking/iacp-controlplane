#!/usr/bin/env python3
"""Prueba coordinada de reinicio SOLO del Redis descartable de compose.test."""
import json
import os
import time
from uuid import uuid4
from urllib.parse import urlsplit

from redis import Redis
from redis.exceptions import ConnectionError, TimeoutError
from iacp.services.bootstrap_secrets import BootstrapSecretError, EphemeralSecrets


def main():
    url = os.environ['IACP_INTEGRATION_REDIS_URL']
    parsed = urlsplit(url)
    if parsed.hostname != 'test-redis' or parsed.path != '/15':
        raise RuntimeError('Solo se permite test-redis y la DB descartable 15')
    redis = Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
    store = EphemeralSecrets(redis, os.urandom(32), 120)
    canary = 'SENUELO-G6-REINICIO-'+uuid4().hex
    reference = None
    try:
        # Replica la persistencia observada en VM 104 sin tocar ese servidor.
        redis.config_set('appendonly', 'yes')
        redis.config_set('appendfsync', 'everysec')
        redis.config_set('save', '3600 1 300 100 60 10000')
        reference = store.put('tenant-fixture', 'instance-fixture', canary)
        key = 'iacp:bootstrap:v1:'+reference
        assert canary.encode() not in redis.dump(key)
        before = redis.ttl(key)
        run_id = redis.info('server')['run_id']
        redis.save()
        print(json.dumps({'estado': 'LISTO_PARA_REINICIAR_REDIS_DESCARTABLE',
            'ttl_antes': before}), flush=True)
        deadline = time.monotonic()+60
        while True:
            try:
                if redis.info('server')['run_id'] != run_id:
                    break
            except (ConnectionError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError('No se observó el reinicio coordinado de test-redis')
            time.sleep(0.1)
        after = redis.ttl(key)
        assert redis.config_get('appendonly')['appendonly'] == 'yes'
        assert 0 < after <= before
        assert store.read(reference, 'tenant-fixture', 'instance-fixture') == canary
        redis.expire(key, 1)
        deadline = time.monotonic()+5
        while redis.exists(key):
            assert time.monotonic() < deadline
            time.sleep(0.1)
        try:
            store.read(reference, 'tenant-fixture', 'instance-fixture')
        except BootstrapSecretError as e:
            assert str(e) == 'BOOTSTRAP_SECRET_EXPIRED_OR_MISSING'
        else:
            raise AssertionError('El secreto caducado no debe poder recuperarse')
        print(json.dumps({'estado': 'PASS', 'recuperado_tras_reinicio': True,
            'appendonly_despues': 'yes',
            'ttl_antes': before, 'ttl_despues': after, 'expiracion_efectiva': True,
            'alcance': 'Redis descartable; no se reinició Redis de VM 104'}), flush=True)
    finally:
        if reference:
            store.delete(reference)
        redis.config_set('save', '')
        redis.config_set('appendonly', 'no')
        redis.close()


if __name__ == '__main__':
    main()
