"""Lab-only root input encryption and bounded, pinned SSH verification.

The user accepted clear initial passwords in private lab plan/state artifacts.
This module never returns, audits or logs a password or provider exception.
"""
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

from ..models import GuestInitialization, Instance, Job, Operation, Tenant, utcnow
from ..security import Principal
from ..terraform.config import PlanError
from ..terraform.locks import ExecutionBusy, instance_lock
from .instances import Conflict, event
from .jobs import QueueUnavailable


SAFE_ACCESS_ERROR_CODES = frozenset({
    'ROOT_ACCESS_BINDING_CHANGED', 'ROOT_ACCESS_CONFIGURATION_MISMATCH',
    'ROOT_ACCESS_EVIDENCE_INVALID', 'CLOUD_INIT_FAILED',
    'ROOT_SSH_IDENTITY_MISMATCH', 'ROOT_SSH_AUTHENTICATION_FAILED',
    'ROOT_SSH_HOST_KEY_MISMATCH',
})


def access_error_code(failure):
    """Convierte fallos de acceso en códigos públicos y sanitizados."""
    import paramiko

    if isinstance(failure, paramiko.AuthenticationException):
        return 'ROOT_SSH_AUTHENTICATION_FAILED'
    if isinstance(failure, paramiko.BadHostKeyException):
        return 'ROOT_SSH_HOST_KEY_MISMATCH'
    code = str(failure) if isinstance(failure, Conflict) else None
    return code if code in SAFE_ACCESS_ERROR_CODES else 'ROOT_ACCESS_VERIFICATION_UNCONFIRMED'


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
    from .guest_identity import technical_program_for_mode
    initialization = session.scalar(select(GuestInitialization).where(
        GuestInitialization.instance_id == instance.id))
    if (initialization is not None
            and initialization.program_sha256 == technical_program_for_mode(initialization.mode)[1]):
        # El acceso técnico ya fue verificado dentro de GuestInitialization.
        # No se crea una segunda operación ni se deriva a commercial_bootstrap.
        if initialization.status != 'completed' or (instance.access_verification or {}).get('status') != 'completed':
            raise Conflict('ROOT_ACCESS_NOT_READY')
        from ..lifecycle.service import response
        operation = session.get(Operation, initialization.operation_id)
        return response(session, operation)
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
    def verify(self, target, instance, *, password=None, ip_addresses=None):
        from .guest_identity import GuestClient
        import paramiko
        client = GuestClient.configured().client
        client.observe(target, preflight=True)
        config = client._request('GET', target.path + '/config?current=1')
        # ``ciuser`` identifica el usuario inicial de Cloud-Init, no el usuario
        # que debe autenticar el contrato de acceso técnico. Las Golden Images
        # Ubuntu usan ``ubuntu`` y pueden habilitar root por SSH de forma
        # explícita durante Guest Initialization.
        if config.get('name') != instance.name:
            raise Conflict('ROOT_ACCESS_CONFIGURATION_MISMATCH')

        def read(path):
            value = client._request('GET', target.path + '/agent/file-read?' + urlencode({'file': path, 'count': 8192}))
            if value.get('truncated'):
                raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID')
            return value['content']

        read('/var/lib/cloud/instance/boot-finished')
        passwd = read('/etc/passwd')
        root_entries = [line.split(':') for line in passwd.splitlines()
                        if line.startswith('root:')]
        if not any(len(fields) > 2 and fields[2] == '0' for fields in root_entries):
            raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID')
        result = json.loads(read('/var/lib/cloud/data/result.json'))
        if result.get('v1', {}).get('errors') != []:
            raise Conflict('CLOUD_INIT_FAILED')
        from .ssh_host_identity import read as read_host_key
        try:
            host_key = read_host_key(client, target, getattr(instance, 'image', None))
        except Exception:
            raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID') from None
        addresses = ip_addresses or instance.ip_addresses
        if not addresses:
            raise Conflict('ROOT_ACCESS_EVIDENCE_INVALID')
        # The host key comes from the same inventory-bound QGA, never TOFU or
        # AutoAddPolicy. IP ownership was verified by DHCP/MAC initialization.
        address = str(ipaddress.IPv4Address(addresses[0]))
        with paramiko.SSHClient() as ssh:
            ssh.get_host_keys().add(address, 'ssh-ed25519', host_key.key)
            ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            ssh.connect(address, username='root', password=password if password is not None else decrypt_password(instance), timeout=10,
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
                    code = access_error_code(failure)
                    operation.status = 'failed' if code in SAFE_ACCESS_ERROR_CODES else 'uncertain'
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
        # La inicialización técnica nueva ya configura y verifica el acceso
        # root dentro de su único fence QGA. Solo las instancias históricas
        # sin ese digest continúan usando la ruta de compatibilidad comercial.
        from .guest_identity import technical_program_for_mode
        if row.program_sha256 == technical_program_for_mode(row.mode)[1]:
            return
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
