"""Entrega efímera AES-256-GCM vinculada a tenant/instancia; nunca usa la DB."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from redis import Redis
from redis.exceptions import RedisError

from ..config import broker_url


class BootstrapSecretError(Exception):
    """Solo códigos públicos estables, sin causas/valores sensibles."""
    def __init__(self,code):
        allowed={'BOOTSTRAP_KEY_UNAVAILABLE','BOOTSTRAP_CONFIGURATION_INVALID',
            'BOOTSTRAP_REFERENCE_INVALID','BOOTSTRAP_REFERENCE_COLLISION',
            'BOOTSTRAP_SECRET_STORE_UNAVAILABLE','BOOTSTRAP_SECRET_EXPIRED_OR_MISSING',
            'BOOTSTRAP_SECRET_BINDING_INVALID','BOOTSTRAP_SECRET_CLEANUP_PENDING',
            'BOOTSTRAP_GUEST_NOT_READY','BOOTSTRAP_SSH_DELIVERY_UNCONFIRMED',
            'BOOTSTRAP_RECONCILIATION_REQUIRED','BOOTSTRAP_HOST_KEY_CHANNEL_UNAVAILABLE',
            'BOOTSTRAP_HOST_KEY_EVIDENCE_INVALID','BOOTSTRAP_CLOUD_INIT_FAILED',
            'BOOTSTRAP_PREFLIGHT_FAILED'}
        super().__init__(code if code in allowed else 'BOOTSTRAP_DELIVERY_UNCONFIRMED')


def configured_key():
    try:
        path=Path(os.environ['IACP_BOOTSTRAP_AES_KEY_FILE'])
        info=path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o027:
            raise ValueError()
        key=path.read_bytes()
        if len(key)!=32:
            raise ValueError()
        return key
    except (OSError,KeyError,ValueError):
        raise BootstrapSecretError('BOOTSTRAP_KEY_UNAVAILABLE') from None


class EphemeralSecrets:
    def __init__(self,redis,key,ttl=900):
        if len(key)!=32 or not isinstance(ttl,int) or isinstance(ttl,bool) or not 60<=ttl<=3600:
            raise BootstrapSecretError('BOOTSTRAP_CONFIGURATION_INVALID')
        self.redis,self._key,self.ttl=redis,key,ttl

    @classmethod
    def configured(cls):
        try:
            ttl=int(os.environ.get('IACP_BOOTSTRAP_SECRET_TTL_SECONDS','900'))
        except ValueError:
            raise BootstrapSecretError('BOOTSTRAP_CONFIGURATION_INVALID') from None
        return cls(Redis.from_url(broker_url(),socket_connect_timeout=3,socket_timeout=3),configured_key(),ttl)

    def fingerprint(self,payload):
        key=hmac.digest(self._key,b'iacp-commercial-idempotency-v1','sha256')
        return hmac.new(key,payload.encode(),hashlib.sha256).hexdigest()

    def _binding(self,reference,tenant_id,instance_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',reference):
            raise BootstrapSecretError('BOOTSTRAP_REFERENCE_INVALID')
        return json.dumps({'reference':reference,'tenant_id':tenant_id,'instance_id':instance_id,
            'version':1},sort_keys=True,separators=(',',':')).encode()

    def put(self,tenant_id,instance_id,password):
        envelope=self.seal(tenant_id,instance_id,password)
        if not self.put_sealed(envelope):raise BootstrapSecretError('BOOTSTRAP_REFERENCE_COLLISION')
        return envelope[0]

    def seal(self,tenant_id,instance_id,password,reference=None):
        reference=reference or secrets.token_urlsafe(32)
        nonce=os.urandom(12)
        aad=self._binding(reference,tenant_id,instance_id)
        ciphertext=AESGCM(self._key).encrypt(nonce,password.encode(),aad)
        # Una única escritura atómica SET NX EX; no ventanas sin caducidad.
        value=base64.b64encode(nonce+ciphertext)
        return reference,value

    def put_sealed(self,envelope):
        reference,value=envelope
        self._binding(reference,'','')
        try:
            return bool(self.redis.set('iacp:bootstrap:v1:'+reference,value,nx=True,ex=self.ttl))
        except RedisError:
            raise BootstrapSecretError('BOOTSTRAP_SECRET_STORE_UNAVAILABLE') from None

    def restore(self,reference,tenant_id,instance_id,password):
        """Restaurar solo desde un replay ya verificado; nunca reemplazar un valor."""
        return self.put_sealed(self.seal(tenant_id,instance_id,password,reference))

    def read(self,reference,tenant_id,instance_id):
        aad=self._binding(reference,tenant_id,instance_id)
        try:
            value=self.redis.get('iacp:bootstrap:v1:'+reference)
        except RedisError:
            raise BootstrapSecretError('BOOTSTRAP_SECRET_STORE_UNAVAILABLE') from None
        if value is None:
            raise BootstrapSecretError('BOOTSTRAP_SECRET_EXPIRED_OR_MISSING')
        try:
            data=base64.b64decode(value,validate=True)
            return AESGCM(self._key).decrypt(data[:12],data[12:],aad).decode()
        except (InvalidTag,ValueError,UnicodeError,TypeError):
            raise BootstrapSecretError('BOOTSTRAP_SECRET_BINDING_INVALID') from None

    def delete(self,reference):
        self._binding(reference,'','')
        try:
            self.redis.delete('iacp:bootstrap:v1:'+reference)
        except RedisError:
            raise BootstrapSecretError('BOOTSTRAP_SECRET_CLEANUP_PENDING') from None
