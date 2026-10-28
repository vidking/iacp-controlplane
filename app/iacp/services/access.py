"""Lab-only root input encryption and bounded, pinned SSH verification.

The user accepted clear initial passwords in private lab plan/state artifacts.
This module never returns, audits or logs a password or provider exception.
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from ..models import Instance, Job, Operation, Tenant, utcnow
from ..security import Principal
from ..terraform.config import PlanError
from ..terraform.locks import ExecutionBusy, instance_lock
from .instances import Conflict, event
from .jobs import QueueUnavailable


def encryption_key():
    try:
        path = Path(os.environ['IACP_ROOT_INPUT_KEY_FILE'])
        # A mounted secret must be private to the service identity/group.
        if path.stat().st_mode & 0o007:
            raise ValueError()
        key = path.read_bytes().strip()
        Fernet(key)
        return key
    except (KeyError, OSError, ValueError):
        raise PlanError('ROOT_INPUT_KEY_UNAVAILABLE') from None


def password_fingerprint(password):
    key = hashlib.sha256(b'iacp-password-uniqueness-v1:' + encryption_key()).digest()
    return hmac.new(key, password.encode(), hashlib.sha256).hexdigest()


def encrypt_password(instance, password):
    payload = json.dumps({'instance_id': instance.id, 'tenant_id': instance.tenant_id, 'password': password}).encode()
    return Fernet(encryption_key()).encrypt(payload).decode()


def decrypt_password(instance):
    try:
        data = json.loads(Fernet(encryption_key()).decrypt(instance.initial_access_ciphertext.encode()))
        if data['instance_id'] != instance.id or data['tenant_id'] != instance.tenant_id:
            raise ValueError()
        if not hmac.compare_digest(password_fingerprint(data['password']), instance.initial_access_fingerprint):
            raise ValueError()
        return data['password']
    except (InvalidToken, ValueError, KeyError, TypeError, AttributeError):
        raise PlanError('ROOT_INPUT_BINDING_CHANGED') from None


def admit_access(spec, tenant_id):
    from ..terraform.profiles import load_profile
    if os.environ.get('IACP_ROOT_PASSWORD_BOOTSTRAP_ENABLED', '0') != '1':
        raise Conflict('ROOT_PASSWORD_BOOTSTRAP_DISABLED')
    if not spec.network_profile:
        raise Conflict('ROOT_ACCESS_REQUIRES_TECHNICAL_PROFILE')
    profile = load_profile(spec.network_profile, tenant_id)
    if not profile.root_password_bootstrap:
        raise Conflict('ROOT_ACCESS_PROFILE_NOT_AUTHORIZED')


def request_verification(session, instance, principal, key, publish):
    from ..models import CommercialIntent
    if session.get(CommercialIntent, instance.id) is not None:
        from .commercial_bootstrap import request
        from ..api.dependencies import get_commercial_bootstrap_publisher
        return request(session,instance,principal,key,get_commercial_bootstrap_publisher())
    from ..lifecycle.service import managed_target, response
    if not instance.initial_access_ciphertext:
        raise Conflict('ROOT_ACCESS_NOT_REQUESTED')
    try:
        with instance_lock(session.get_bind(), instance.id):
            instance = session.get(Instance, instance.id, populate_existing=True)
            managed_target(session, instance)
            previous = session.scalar(select(Operation).where(Operation.tenant_id == principal.tenant_id, Operation.idempotency_key == key))
            if previous:
                if (previous.instance_id != instance.id or previous.kind != 'instance.access.verify'
                        or previous.requested_by != principal.subject):
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                operation = previous
                if operation.status in {'failed', 'uncertain'}:
                    operation.status = 'queued'
                    session.scalar(select(Job).where(Job.operation_id == operation.id)).status = 'queued'
            else:
                if session.scalar(select(Operation.id).where(Operation.instance_id == instance.id,
                        Operation.kind == 'instance.access.verify', Operation.status.in_(['queued', 'running']))):
                    raise Conflict('ROOT_ACCESS_VERIFICATION_ACTIVE')
                operation = Operation(id=str(uuid4()), instance_id=instance.id, tenant_id=instance.tenant_id,
                    kind='instance.access.verify', status='queued', requested_by=principal.subject,
                    idempotency_key=key, request_hash=hashlib.sha256(instance.id.encode()).hexdigest())
                session.add(operation); session.flush()
                session.add(Job(id=str(uuid4()), operation=operation.kind, tenant_id=instance.tenant_id,
                    operation_id=operation.id, status='queued'))
                event(session, operation, 'access_verification_accepted', principal.subject)
            session.commit()
            result = response(session, operation)
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None
    if operation.status in {'queued', 'running'}:
        try:
            publish(operation.id)
        except Exception:
            raise QueueUnavailable from None
    return result


class AccessVerifier:
    def verify(self, target, instance):
        from .guest_identity import GuestClient
        import paramiko
        client = GuestClient.configured().client
        client.observe(target, preflight=True)
        config = client._request('GET', target.path + '/config?current=1')
        if config.get('ciuser') != 'root' or config.get('name') != instance.name:
            raise Conflict('ROOT_ACCESS_CONFIGURATION_MISMATCH')

        def read(path):
            value = client._request('GET', target.path + '/agent/file-read?' + urlencode({'file': path, 'count': 8192}))
            if value.get('truncated'):
                raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID')
            return value['content']

        read('/var/lib/cloud/instance/boot-finished')
        result = json.loads(read('/var/lib/cloud/data/result.json'))
        if result.get('v1', {}).get('errors') != []:
            raise Conflict('CLOUD_INIT_FAILED')
        host_key = read('/etc/ssh/ssh_host_ed25519_key.pub').split()
        if host_key[0] != 'ssh-ed25519' or not instance.ip_addresses:
            raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID')
        # The host key comes from the same inventory-bound QGA, never TOFU or
        # AutoAddPolicy. IP ownership was verified by DHCP/MAC initialization.
        address = str(ipaddress.IPv4Address(instance.ip_addresses[0]))
        key = paramiko.Ed25519Key(data=base64.b64decode(host_key[1], validate=True))
        with paramiko.SSHClient() as ssh:
            ssh.get_host_keys().add(address, 'ssh-ed25519', key)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            ssh.connect(address, username='root', password=decrypt_password(instance), timeout=10,
                auth_timeout=10, banner_timeout=10, allow_agent=False, look_for_keys=False)
            _, stdout, _ = ssh.exec_command('/usr/bin/id -u', timeout=5)
            if stdout.read(32).strip() != b'0' or stdout.channel.recv_exit_status() != 0:
                raise Conflict('ROOT_SSH_IDENTITY_MISMATCH')
        return {'status': 'completed', 'cloud_init_complete': True, 'ssh_authenticated': True,
            'observed_at': utcnow().isoformat(), 'error_code': None}


def execute_verification(identifier, sessions, verifier=None):
    from ..lifecycle.service import managed_target
    with sessions() as session:
        operation = session.get(Operation, identifier)
        if not operation or operation.kind != 'instance.access.verify':
            return
        instance_id = operation.instance_id
    try:
        with instance_lock(sessions.kw['bind'], instance_id) as heartbeat:
            with sessions() as session:
                operation = session.get(Operation, identifier, with_for_update=True)
                if operation.status not in {'queued', 'running'}:
                    return
                instance = session.get(Instance, instance_id)
                job = session.scalar(select(Job).where(Job.operation_id == identifier))
                try:
                    tenant = session.get(Tenant, operation.tenant_id)
                    if (not tenant or not tenant.active or not job or instance.tenant_id != operation.tenant_id
                            or job.tenant_id != operation.tenant_id or job.operation != operation.kind
                            or not instance.initial_access_ciphertext):
                        raise Conflict('ROOT_ACCESS_BINDING_CHANGED')
                    target, _ = managed_target(session, instance)
                    from .technical_ownership import is_managed_vmid
                    if not is_managed_vmid(target.vmid):
                        raise Conflict('ROOT_ACCESS_BINDING_CHANGED')
                    operation.status = job.status = 'running'
                    session.commit()
                    result = (verifier or AccessVerifier()).verify(target, instance)
                    if result.get('cloud_init_complete') is not True or result.get('ssh_authenticated') is not True:
                        raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID')
                    heartbeat()
                    # Publish only the fixed allowlist; never verifier/provider output.
                    instance.access_verification = {k: result[k] for k in ('status', 'cloud_init_complete', 'ssh_authenticated', 'observed_at')}
                    instance.access_verification['status'] = 'completed'
                    operation.status = job.status = 'completed'; operation.error_code = None
                    event(session, operation, 'root_cloud_init_ssh_verified', 'worker:iac')
                except Exception as failure:
                    import paramiko
                    safe_codes = {'ROOT_ACCESS_BINDING_CHANGED', 'ROOT_ACCESS_CONFIGURATION_MISMATCH',
                        'ROOT_ACCESS_EVIDENCE_INVALID', 'CLOUD_INIT_FAILED', 'ROOT_SSH_IDENTITY_MISMATCH',
                        'ROOT_SSH_AUTHENTICATION_FAILED', 'ROOT_SSH_HOST_KEY_MISMATCH'}
                    code = str(failure) if isinstance(failure, Conflict) and str(failure) in safe_codes else 'ROOT_ACCESS_VERIFICATION_UNCONFIRMED'
                    if isinstance(failure, paramiko.AuthenticationException):
                        code = 'ROOT_SSH_AUTHENTICATION_FAILED'
                    elif isinstance(failure, paramiko.BadHostKeyException):
                        code = 'ROOT_SSH_HOST_KEY_MISMATCH'
                    operation.status = 'failed' if code in safe_codes else 'uncertain'
                    if job:
                        job.status = 'failed'
                    operation.error_code = code
                    instance.access_verification = {'status': 'uncertain', 'error_code': operation.error_code}
                    event(session, operation, 'root_access_verification_unconfirmed', 'worker:iac')
                session.commit()
    except ExecutionBusy:
        return


def after_initialization(identifier, sessions):
    from ..api.dependencies import get_access_verification_publisher
    from ..models import GuestInitialization
    with sessions() as session:
        row = session.get(GuestInitialization, identifier)
        if not row or row.status != 'completed':
            return
        instance = session.get(Instance, row.instance_id)
        from ..models import CommercialIntent
        if session.get(CommercialIntent, instance.id) is not None:
            from .commercial_bootstrap import after_initialization as bootstrap
            bootstrap(instance.id,sessions)
            return
        if not instance.initial_access_ciphertext:
            return
        creation = session.scalar(select(Operation).where(Operation.instance_id == instance.id, Operation.kind == 'instance.create'))
        try:
            request_verification(session, instance, Principal(instance.tenant_id, creation.requested_by, 'operator'),
                'access-verify-' + instance.id, get_access_verification_publisher())
        except Exception:
            session.rollback()
            event(session, session.get(Operation, creation.id), 'access_verification_paused', 'worker:iac')
            session.commit()
