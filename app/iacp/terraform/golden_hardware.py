"""Hardware heredado de la Golden Image, observado y sellado antes del plan."""
import re
import ssl
from urllib.parse import quote
from urllib.request import build_opener, ProxyHandler, HTTPSHandler

from pydantic import Field
from .config import PlanError, StrictModel
from .observation import NoRedirect


class GoldenHardware(StrictModel):
    template_vm_id: int
    template_node: str
    cpu_type: str = Field(pattern=r'^[A-Za-z0-9_.:-]+$')
    flags: list[str]
    numa: bool
    sockets: int = Field(ge=1)
    source_sockets: int = Field(ge=1)
    source_cpu: str
    base: dict


def read(env, allocation, vcpus):
    from ..lifecycle.proxmox import ProxmoxPower, PowerError
    from ..catalog import resolve_image
    image=resolve_image(allocation.image)
    node=allocation.template_node_name or allocation.node_name
    if (allocation.template_vm_id!=image.template_vm_id or node!=image.source_node
            or allocation.environment!='lab' or allocation.node_name not in {'mng01','mng02'}):
        raise PlanError('GOLDEN_HARDWARE_SCOPE_INVALID')
    client=ProxmoxPower(env['PROXMOX_VE_ENDPOINT'].rstrip('/'),env['PROXMOX_VE_API_TOKEN'],
        build_opener(ProxyHandler({}),HTTPSHandler(context=ssl.create_default_context(cafile=env.get('SSL_CERT_FILE'))),NoRedirect()))
    try:
        raw=client._request('GET',f'/nodes/{quote(node,safe="")}/qemu/{image.template_vm_id}/config?current=1')
        return parse(raw,image.template_vm_id,node,vcpus)
    except PowerError:
        raise PlanError('GOLDEN_HARDWARE_READ_UNAVAILABLE') from None


def parse(raw, vmid, node, vcpus):
    try:
        if raw.get('template')!=1:raise ValueError()
        parts=raw['cpu'].split(',');options=dict(part.split('=',1) for part in parts[1:])
        if set(options)-{'flags'}:raise ValueError('Unsupported CPU options')
        flags=options.get('flags','').split(';') if options.get('flags') else []
        if any(not re.fullmatch(r'[+-][A-Za-z0-9_.-]+',flag) for flag in flags):raise ValueError()
        if any(re.fullmatch(r'numa\d+',key) for key in raw):raise ValueError('Custom NUMA topology requires an explicit adapter')
        source_sockets=int(raw.get('sockets',1))
        if source_sockets<1 or vcpus<1:raise ValueError()
        sockets=source_sockets if vcpus%source_sockets==0 and vcpus>=source_sockets else 1
        numa=raw.get('numa',0)
        if numa not in (0,1):raise ValueError()
        if 'memory' in (raw.get('hotplug') or '').split(',') and not numa:
            raise PlanError('GOLDEN_MEMORY_HOTPLUG_REQUIRES_NUMA')
        base={key:raw.get(key) for key in ['bios','machine','scsihw','hotplug','boot']}
        return GoldenHardware(template_vm_id=vmid,template_node=node,cpu_type=parts[0],flags=flags,
            numa=bool(numa),sockets=sockets,source_sockets=source_sockets,source_cpu=raw['cpu'],base=base)
    except (KeyError,TypeError,ValueError):
        raise PlanError('GOLDEN_HARDWARE_UNSUPPORTED') from None


def verify_cpu(actual, variables):
    hardware=variables.get('iacp_golden_hardware')
    if not hardware:return
    cpu=actual.get('cpu',[{}])[0]
    if (cpu.get('type')!=hardware['cpu_type'] or sorted(cpu.get('flags') or [])!=sorted(hardware['flags'])
            or cpu.get('numa') is not hardware['numa'] or cpu.get('sockets',1)!=hardware['sockets']
            or cpu.get('cores',0)*cpu.get('sockets',1)!=variables['instance']['cpu_cores']):
        raise PlanError('GOLDEN_HARDWARE_PLAN_MISMATCH')


def verify_live(config, hardware, expected):
    try:
        parts=config['cpu'].split(',');options=dict(part.split('=',1) for part in parts[1:])
        flags=options.get('flags','').split(';') if options.get('flags') else []
        if (parts[0]!=hardware['cpu_type'] or sorted(flags)!=sorted(hardware['flags'])
                or int(config.get('numa',0)) not in (0,1) or bool(int(config.get('numa',0)))!=hardware['numa']
                or int(config.get('sockets',1))!=hardware['sockets']
                or int(config.get('cores',1))*hardware['sockets']!=expected['cpu_cores']):
            raise ValueError()
        for name,value in hardware['base'].items():
            actual=config.get(name)
            if name=='hotplug' and actual is not None and value is not None:
                if set(actual.split(','))!=set(value.split(',')):raise ValueError()
            elif actual!=value:raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise PlanError('GOLDEN_HARDWARE_POSTCONDITION_MISMATCH') from None
