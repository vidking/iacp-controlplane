"""Authenticated asynchronous guest-network discovery; no network/state mutations."""
from uuid import uuid4
import ipaddress
import time
from datetime import timedelta

from sqlalchemy import select

from ..models import Instance, Job, NetworkDiscovery, Operation, Tenant, TerraformExecution, utcnow
from ..lifecycle.certification_binding import binding_digest
from ..lifecycle.service import managed_target
from .instances import Conflict,event
from .jobs import QueueUnavailable
from ..terraform.apply import fingerprint
from ..terraform.locks import ExecutionBusy,instance_lock


# QGA and systemd-networkd can be unavailable for a short interval after a
# START/REBOOT.  Discovery is deliberately bounded, but a transient empty
# answer is never a successful observation.
DISCOVERY_TIMEOUT_SECONDS = 120
DISCOVERY_MAX_ATTEMPTS = 18


def _last_verified(instance):
    observation = instance.network_observation or {}
    if observation.get('status') == 'observed':
        addresses = observation.get('ip_addresses') or instance.ip_addresses or []
        verified_at = observation.get('verified_at') or observation.get('observed_at')
    else:
        addresses = (observation.get('last_known_ip_addresses')
                     or observation.get('ip_addresses')
                     or instance.ip_addresses or [])
        verified_at = observation.get('last_known_verified_at')
    return sorted(set(addresses)), verified_at


def _pending_observation(instance, result, *, attempt=None, deadline=None):
    addresses, verified_at = _last_verified(instance)
    observation = {**result, 'status': 'pending', 'ip_addresses': [],
                   'current_ip_addresses': [], 'current_ip_verified': False}
    if addresses:
        observation['last_known_ip_addresses'] = addresses
    if verified_at:
        observation['last_known_verified_at'] = verified_at
    if attempt is not None:
        observation['attempt'] = attempt
    if deadline is not None:
        observation['deadline_at'] = deadline
    return observation


def _observed_observation(instance, result):
    addresses = sorted(set(result.get('ip_addresses') or []))
    previous, _ = _last_verified(instance)
    observed_at = result.get('observed_at')
    observation = {**result, 'status': 'observed', 'ip_addresses': addresses,
                   'current_ip_addresses': addresses, 'current_ip_verified': True,
                   'last_known_ip_addresses': addresses,
                   'last_known_verified_at': observed_at}
    if previous and previous != addresses:
        observation['previous_ip_addresses'] = previous
    return observation


def _store_pending(instance, row, result, *, attempt, deadline):
    observation = _pending_observation(instance, result, attempt=attempt, deadline=deadline)
    if row is not None:
        row.result = observation
    instance.network_observation = observation
    # An empty value means "no current verified address".  The previous value
    # is retained in network_observation.last_known_ip_addresses instead of
    # being presented as live connectivity.
    instance.ip_addresses = []
    return observation


def _store_observed(instance, row, result):
    observation = _observed_observation(instance, result)
    row.result = instance.network_observation = observation
    instance.ip_addresses = observation['ip_addresses']
    return observation


def _backoff(attempt):
    return min(10, 2 ** min(max(attempt - 1, 0), 3))


def request_discovery(session,instance,principal,key,publish):
    if instance.network_mode!='dhcp':
        raise Conflict('DHCP_PROFILE_REQUIRED')
    digest=fingerprint({'instance_id':instance.id,'action':'network.discover'})
    try:
        with instance_lock(session.get_bind(),instance.id):
            instance=session.get(Instance,instance.id,populate_existing=True)
            previous=session.scalar(select(Operation).where(Operation.tenant_id==principal.tenant_id,Operation.idempotency_key==key))
            if previous:
                if previous.kind!='instance.network.discover' or previous.request_hash!=digest or previous.requested_by!=principal.subject:
                    raise Conflict('IDEMPOTENCY_CONFLICT')
                operation=previous
                result=accepted(session,previous)
                publish_needed=previous.status in {'queued','running'}
                session.commit()
            else:
                target,approval=managed_target(session,instance)
                binding=binding_digest(instance,session.get(TerraformExecution,instance.id),approval)
                operation=Operation(id=str(uuid4()),tenant_id=instance.tenant_id,instance_id=instance.id,kind='instance.network.discover',
                                    status='queued',idempotency_key=key,request_hash=digest,requested_by=principal.subject)
                session.add(operation);session.flush()
                session.add(Job(id=str(uuid4()),operation=operation.kind,operation_id=operation.id,tenant_id=instance.tenant_id,status='queued'))
                session.add(NetworkDiscovery(operation_id=operation.id,instance_id=instance.id,provisioning_binding=binding))
                event(session,operation,'network_discovery_accepted',principal.subject);session.commit()
                result=accepted(session,operation);publish_needed=True
    except ExecutionBusy:
        raise Conflict('INSTANCE_BUSY') from None
    if publish_needed:
        try:publish(operation.id)
        except Exception:raise QueueUnavailable from None
    return result


def accepted(session,operation):
    job=session.scalar(select(Job).where(Job.operation_id==operation.id))
    return {'instance_id':operation.instance_id,'operation_id':operation.id,'job_id':job.id,'status':operation.status}


def observe_guest(execution,instance):
    """Use the worker's existing TF token with GuestAgent.Audit, never API secrets."""
    import ssl
    from urllib.request import build_opener,ProxyHandler,HTTPSHandler
    from ..terraform.lifecycle import build_executor
    from ..lifecycle.proxmox import ProxmoxPower,PowerError
    from ..terraform.observation import NoRedirect
    import tempfile
    from pathlib import Path
    expected_mac=(instance.result or {}).get('network',{}).get('mac_address')
    if not expected_mac:
        return {'status':'pending','source':'qemu-guest-agent','ip_addresses':[],'reason':'MAC_NOT_OBSERVED'}
    with tempfile.TemporaryDirectory() as temporary:
        env=build_executor(observation=True).environment(Path(temporary))
        client=ProxmoxPower(env['PROXMOX_VE_ENDPOINT'].rstrip('/'),env['PROXMOX_VE_API_TOKEN'],
            build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=env.get('SSL_CERT_FILE'))),NoRedirect()))
        path=f'/nodes/{instance.node}/qemu/{instance.vmid}'
        try:
            config=client._request('GET',path+'/config?current=1')
            nic=dict(part.split('=',1) for part in config.get('net0','').split(',') if '=' in part)
            expected=execution.allocation
            ip=dict(part.split('=',1) for part in config.get('ipconfig0','').split(',') if '=' in part)
            vlan=expected['vlan_id']
            if (config.get('name')!=instance.name or nic.get('virtio','').lower()!=expected_mac.lower()
                    or nic.get('bridge')!=expected['network_bridge']
                    or (vlan is None and ('tag' in nic or 'trunks' in nic))
                    or (vlan is not None and nic.get('tag')!=str(vlan))
                    or ip.get('ip')!='dhcp' or 'gw' in ip):
                raise Conflict('NETWORK_BINDING_CHANGED')
            result=client._request('GET',path+'/agent/network-get-interfaces')
        except PowerError:
            return {'status':'pending','source':'qemu-guest-agent','ip_addresses':[],'reason':'GUEST_AGENT_UNAVAILABLE'}
    return agent_addresses(result,expected_mac)


def agent_addresses(result,expected_mac):
    rows=result.get('result',[]) if isinstance(result,dict) else result
    if not isinstance(rows,list):rows=[]
    addresses=[]
    for interface in rows:
        if interface.get('hardware-address','').lower()!=expected_mac.lower():continue
        for entry in interface.get('ip-addresses',[]):
            try:address=ipaddress.ip_address(entry['ip-address'])
            except (ValueError,KeyError):continue
            if address.version==4 and not (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified):
                addresses.append(str(address))
    return {'status':'observed' if addresses else 'pending','source':'qemu-guest-agent','ip_addresses':sorted(set(addresses)),
            'mac_address':expected_mac}


def execute_discovery(identifier, sessions, observer=observe_guest,
                      timeout=DISCOVERY_TIMEOUT_SECONDS, pause=time.sleep):
    """Observe DHCP until a verified address exists or the real timeout expires.

    A pending QGA/DHCP answer keeps the operation running and is persisted as
    pending.  It is never converted to a successful empty observation.
    """
    with sessions() as s:
        row=s.get(NetworkDiscovery,identifier)
        if row is None:return
        instance_id=row.instance_id
    try:
        with instance_lock(sessions.kw['bind'],instance_id) as heartbeat:
            with sessions() as s:
                operation=s.get(Operation,identifier,with_for_update=True)
                if operation.status not in {'queued','running'}:return
                row=s.get(NetworkDiscovery,identifier);instance=s.get(Instance,instance_id)
                job=s.scalar(select(Job).where(Job.operation_id==identifier))
                operation.status=job.status='running';s.commit()
                deadline=time.monotonic()+max(float(timeout),0)
                deadline_at=(utcnow()+timedelta(seconds=max(float(timeout),0))).isoformat()
                attempt=0
                pending_event=False
                try:
                    tenant=s.get(Tenant,instance.tenant_id)
                    if (not tenant or not tenant.active or operation.tenant_id!=instance.tenant_id or job.tenant_id!=instance.tenant_id
                            or instance.network_mode!='dhcp' or operation.kind!='instance.network.discover' or job.operation!=operation.kind):
                        raise Conflict('TENANT_OR_NETWORK_BINDING_CHANGED')
                    target,approval=managed_target(s,instance);execution=s.get(TerraformExecution,instance.id)
                    if row.provisioning_binding!=binding_digest(instance,execution,approval):raise Conflict('NETWORK_BINDING_CHANGED')
                    while True:
                        attempt += 1
                        s.refresh(instance)
                        if instance.status!='ready':raise Conflict('NETWORK_BINDING_CHANGED')
                        # Network observation is read-only and independent from
                        # guest identity/SSH initialization.  QGA can prove a
                        # DHCP address while a separate guest-exec operation is
                        # still pending, failed, or awaiting reconciliation.
                        try:
                            result=observer(execution,instance)
                        except Exception as failure:
                            from ..lifecycle.proxmox import PowerError
                            if isinstance(failure,PowerError):
                                result={'status':'pending','source':'qemu-guest-agent','ip_addresses':[],
                                        'reason':failure.code}
                            else:
                                raise
                        if not isinstance(result,dict):raise Conflict('NETWORK_DISCOVERY_INVALID_RESULT')
                        addresses=sorted(set(result.get('ip_addresses') or []))
                        if addresses:
                            others=s.scalars(select(Instance).where(Instance.id!=instance.id,Instance.status!='deleted',
                                Instance.network_mode=='dhcp')).all()
                            if any(set(addresses) & set(other.ip_addresses or []) for other in others):
                                result={**result,'status':'pending','ip_addresses':[],
                                        'reason':'DHCP_ADDRESS_CONFLICT'}
                        result={**result,'ip_addresses':addresses,'observed_at':utcnow().isoformat(),
                                'attempt':attempt}
                        if result.get('status')=='observed' and addresses:
                            _store_observed(instance,row,result)
                            operation.status=job.status='completed';operation.error_code=None
                            event(s,operation,'network_observed','worker:terraform');s.commit();return

                        observation=_store_pending(instance,row,result,attempt=attempt,
                                                  deadline=deadline_at)
                        if not pending_event:
                            event(s,operation,'network_pending','worker:terraform')
                            pending_event=True
                        operation.status=job.status='running'
                        s.commit()
                        heartbeat()
                        if observation.get('reason')=='DHCP_ADDRESS_CONFLICT':
                            operation.status=job.status='failed';operation.error_code='DHCP_ADDRESS_CONFLICT'
                            event(s,operation,'network_discovery_failed','worker:terraform');s.commit();return
                        if attempt>=DISCOVERY_MAX_ATTEMPTS or time.monotonic()>=deadline:
                            observation={**observation,'reason':'NETWORK_DISCOVERY_TIMEOUT',
                                         'last_pending_reason':result.get('reason')}
                            row=s.get(NetworkDiscovery,identifier)
                            row.result=instance.network_observation=observation
                            instance.ip_addresses=[]
                            operation.status=job.status='failed';operation.error_code='NETWORK_DISCOVERY_TIMEOUT'
                            event(s,operation,'network_discovery_timeout','worker:terraform');s.commit();return
                        pause(min(_backoff(attempt),max(deadline-time.monotonic(),0)))
                except Exception as failure:
                    operation.status=job.status='failed'
                    operation.error_code=(str(failure)[:64] if isinstance(failure,Conflict)
                        else 'NETWORK_DISCOVERY_FAILED')
                    event(s,operation,'network_discovery_failed','worker:terraform');s.commit()
    except ExecutionBusy:return


def after_power(identifier,sessions):
    """Queue independent guest initialization and network observation.

    A lifecycle operation must never make network visibility depend on the
    outcome of guest-exec.  Both follow-ups are best-effort and independently
    auditable; neither one changes power, Terraform, or guest configuration.
    """
    from ..security import Principal
    with sessions() as s:
        operation=s.get(Operation,identifier)
        if not operation or operation.status!='completed' or operation.kind not in {'instance.power.start','instance.power.reboot'}:return
        instance=s.get(Instance,operation.instance_id)
        if instance.network_mode!='dhcp':return
        policy=None
        from ..models import LabPolicy
        policy=s.get(LabPolicy,instance.self_service_policy_id) if instance.self_service_policy_id else None
        if instance.commercial_revision:
            from .commercial import delegation
            policy=delegation(s,instance,'start')

        initialization_failure=None
        if policy and policy.profile_snapshots.get(instance.network_profile,{}).get('guest_initialization'):
            try:
                from .guest_identity import request_initialization
                from ..api.dependencies import get_guest_initialization_publisher
                from ..models import GuestInitialization
                initialization=s.scalar(select(GuestInitialization).where(GuestInitialization.instance_id==instance.id))
                if initialization is None:
                    request_initialization(s,instance,Principal(instance.tenant_id,operation.requested_by,'operator'),
                        'identity-'+instance.id,get_guest_initialization_publisher(),current_profiles=False)
                elif initialization.status!='completed':
                    if initialization.status=='failed' and not initialization.started_at:
                        previous=s.get(Operation,initialization.operation_id)
                        request_initialization(s,instance,Principal(instance.tenant_id,previous.requested_by,'operator'),
                            previous.idempotency_key,get_guest_initialization_publisher(),current_profiles=False)
                    elif initialization.status!='failed':
                        get_guest_initialization_publisher()(initialization.operation_id)
            except Exception as failure:
                # A guest-exec dispatch failure must not suppress the
                # independent, read-only network observation below.
                s.rollback()
                initialization_failure=failure

        network_failure=None
        try:
            s.expire_all()
            operation=s.get(Operation,identifier)
            instance=s.get(Instance,operation.instance_id)
            from ..api.dependencies import get_network_publisher
            # The power operation is part of the idempotency scope.  A stable
            # per-instance key would incorrectly reuse a completed discovery
            # after a later REBOOT and leave a changed DHCP address stale.
            request_discovery(s,instance,Principal(instance.tenant_id,operation.requested_by,'operator'),
                              'network-after-'+identifier+'-'+operation.id,get_network_publisher())
        except Exception as failure:
            s.rollback()
            network_failure=failure

        if initialization_failure or network_failure:
            operation=s.get(Operation,identifier)
            if operation:
                if initialization_failure:
                    event(s,operation,'guest_initialization_followup_unconfirmed','worker:lifecycle')
                if network_failure:
                    reason = (str(network_failure)[:64] if isinstance(network_failure, Conflict)
                              else 'NETWORK_FOLLOWUP_DISPATCH_UNCONFIRMED')
                    event(s,operation,'network_followup_dispatch_unconfirmed','worker:lifecycle')
                    instance=s.get(Instance,operation.instance_id)
                    if instance:
                        _store_pending(instance, None, {
                            'source':'qemu-guest-agent','ip_addresses':[],
                            'reason':reason}, attempt=0,
                            deadline=utcnow().isoformat())
                s.commit()
