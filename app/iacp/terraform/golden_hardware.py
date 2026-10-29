"""Hardware heredado de la Golden Image, observado y sellado antes del plan."""
import re
import ssl
from typing import Literal
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
    # Representación normalizada que utiliza el provider Terraform. ``base``
    # conserva los nombres y valores crudos de Proxmox para la comprobación
    # posterior; este mapa evita que el adaptador invente conversiones.
    terraform_base: dict | None = None
    # Snapshot técnico del disco de arranque. Es opcional únicamente para
    # snapshots históricos anteriores a la captura de hardware de disco.
    disk: "GoldenDisk | None" = None
    # Etiqueta de identidad de la imagen que debe conservar el clon. Es
    # opcional para mantener compatibles los snapshots históricos que fueron
    # sellados antes de observar tags en la Golden Image.
    vmtag: str | None = Field(default=None, pattern=r"^ubuntu-(?:2204|2404|2604)$")


class GoldenDisk(StrictModel):
    """Atributos del disco realmente presentes en la Golden Image.

    Los ``None`` son significativos: indican que Proxmox no publicó el
    atributo en la plantilla y, por tanto, Terraform no debe introducir un
    valor por defecto del provider.
    """

    interface: str = Field(pattern=r"^(scsi([0-9]|[12][0-9]|30)|sata[0-5]|virtio([0-9]|1[0-5]))$")
    source_datastore_id: str = Field(min_length=1)
    # Evidencia del backend de almacenamiento observada en Proxmox. Las
    # equivalencias de defaults solo se admiten para backends verificados.
    storage_type: str | None = None
    storage_krbd: bool | None = None
    minimum_size_gb: int = Field(ge=1)
    file_format: Literal["raw", "qcow2", "vmdk"] | None = None
    aio: Literal["io_uring", "native", "threads"] | None = None
    cache: Literal["none", "writethrough", "writeback", "directsync", "unsafe"] | None = None
    discard: Literal["on", "ignore"] | None = None
    backup: bool | None = None
    replicate: bool | None = None
    iothread: bool | None = None
    ssd: bool | None = None
    serial: str | None = None
    queues: int | None = Field(default=None, ge=1)


_DISK_OPTION_KEYS = {
    "aio", "backup", "cache", "discard", "iothread", "replicate", "ssd",
    "serial", "queues", "size", "format",
}
_DISK_TECHNICAL_FIELDS = (
    "file_format", "aio", "cache", "discard", "backup", "replicate",
    "iothread", "ssd", "serial", "queues",
)

_VMTAG_IMAGES = frozenset({"ubuntu-2204", "ubuntu-2404", "ubuntu-2604"})


def _image_vmtag(raw, image_id):
    """Obtiene la etiqueta de identidad Ubuntu directamente de Proxmox.

    No se copian tags administrativos como ``golden-image`` o la versión de
    la plantilla. Solo se hereda el tag que identifica la imagen solicitada y
    únicamente para las imágenes Ubuntu autorizadas por este adaptador.
    """
    if image_id not in _VMTAG_IMAGES:
        return None
    raw_tags = raw.get("tags")
    if not isinstance(raw_tags, str):
        raise ValueError()
    tags = {item for item in raw_tags.split(";") if item}
    if image_id not in tags:
        raise ValueError()
    return image_id


def _storage_observation(client, node, storage_id):
    """Read storage metadata using the Terraform identity's approved scope."""
    encoded = quote(storage_id, safe="")
    try:
        # The global storage endpoint requires Datastore.Audit at the storage
        # path.  The Terraform identity is intentionally scoped to the
        # node-level read-only endpoint instead.
        storage = client._request('GET', f'/storage/{encoded}')
    except Exception as failure:
        # Avoid importing the Proxmox client at module import time.  Only its
        # stable permission error is eligible for the node-scoped fallback.
        if getattr(failure, 'code', None) != 'POWER_PERMISSION_DENIED':
            raise
        storage = client._request(
            'GET', f'/nodes/{quote(node, safe="")}/storage/{encoded}/status')
    if not isinstance(storage, dict) or not isinstance(storage.get('type'), str):
        raise ValueError()
    # PVE omits the optional krbd field when it is disabled.  Treat that
    # documented default as false; any other value is rejected by read().
    krbd = storage.get('krbd', False)
    if krbd not in (False, True, 0, 1, '0', '1'):
        raise ValueError()
    return storage['type'], bool(krbd in (True, 1, '1')) if storage['type'] == 'rbd' else None


def _bool_option(value):
    if value not in {"0", "1"}:
        raise ValueError()
    return value == "1"


def _disk(raw_value, interface, *, allow_default_queues=False):
    if not isinstance(raw_value, str):
        raise ValueError()
    parts = raw_value.split(",")
    if not parts or ":" not in parts[0]:
        raise ValueError()
    source_datastore_id = parts[0].split(":", 1)[0]
    if not source_datastore_id:
        raise ValueError()
    options = {}
    for item in parts[1:]:
        if "=" not in item:
            raise ValueError()
        key, value = item.split("=", 1)
        if key not in _DISK_OPTION_KEYS or key in options:
            # No ignorar silenciosamente una opción técnica que el provider
            # no sabe materializar.
            raise ValueError()
        options[key] = value
    size = options.pop("size", None)
    if size is None:
        raise ValueError()
    match = re.fullmatch(r"([1-9][0-9]*)G", size)
    if not match:
        raise ValueError()
    fmt = options.pop("format", None)
    if fmt is not None and fmt not in {"raw", "qcow2", "vmdk"}:
        raise ValueError()
    queues = options.pop("queues", None)
    if queues is not None:
        queues = int(queues)
        if queues == 0 and allow_default_queues:
            queues = None
        elif queues < 1:
            raise ValueError()
    return GoldenDisk(
        interface=interface,
        source_datastore_id=source_datastore_id,
        minimum_size_gb=int(match.group(1)),
        file_format=fmt,
        aio=options.pop("aio", None),
        cache=options.pop("cache", None),
        discard=options.pop("discard", None),
        backup=_bool_option(options.pop("backup")) if "backup" in options else None,
        replicate=_bool_option(options.pop("replicate")) if "replicate" in options else None,
        iothread=_bool_option(options.pop("iothread")) if "iothread" in options else None,
        ssd=_bool_option(options.pop("ssd")) if "ssd" in options else None,
        serial=options.pop("serial", None),
        queues=queues,
    )


def _boot_disk_interface(raw):
    boot = raw.get("boot")
    if isinstance(boot, str):
        match = re.search(r"(?:^|,)order=([^,]+)", boot)
        if match:
            candidate = match.group(1).split(";", 1)[0]
            if re.fullmatch(r"(?:scsi([0-9]|[12][0-9]|30)|sata[0-5]|virtio([0-9]|1[0-5]))", candidate):
                return candidate
    candidates = sorted(key for key in raw if re.fullmatch(r"(?:scsi([0-9]|[12][0-9]|30)|sata[0-5]|virtio([0-9]|1[0-5]))", key))
    if not candidates:
        raise ValueError()
    return candidates[0]


def _boot_order(raw):
    """Convierte ``boot=order=scsi0`` al atributo ``boot_order`` del provider."""
    boot = raw.get("boot")
    if not isinstance(boot, str):
        return None
    match = re.search(r"(?:^|,)order=([^,]+)", boot)
    if not match:
        return None
    order = [item for item in match.group(1).split(";") if item]
    return order or None


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
        hardware = parse(raw,image.template_vm_id,node,vcpus,image.image_id)
        if hardware.disk is not None:
            try:
                storage_type, storage_krbd = _storage_observation(
                    client, node, hardware.disk.source_datastore_id)
            except (PowerError, ValueError):
                raise PowerError('invalid storage observation') from None
            hardware = hardware.model_copy(update={
                'disk': hardware.disk.model_copy(update={
                    'storage_type': storage_type,
                    'storage_krbd': storage_krbd,
                })
            })
        return hardware
    except PowerError:
        raise PlanError('GOLDEN_HARDWARE_READ_UNAVAILABLE') from None


def parse(raw, vmid, node, vcpus, image_id=None):
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
        terraform_base={
            'bios': raw.get('bios'),
            'machine': raw.get('machine'),
            'scsi_hardware': raw.get('scsihw'),
            'hotplug': raw.get('hotplug'),
            'boot_order': _boot_order(raw),
        }
        interface = _boot_disk_interface(raw)
        disk = _disk(raw.get(interface), interface)
        return GoldenHardware(template_vm_id=vmid,template_node=node,cpu_type=parts[0],flags=flags,
            numa=bool(numa),sockets=sockets,source_sockets=source_sockets,source_cpu=raw['cpu'],base=base,
            terraform_base=terraform_base,
            disk=disk, vmtag=_image_vmtag(raw, image_id))
    except (KeyError,TypeError,ValueError):
        raise PlanError('GOLDEN_HARDWARE_UNSUPPORTED') from None


def verify_cpu(actual, variables, *, allow_historical_hotplugged_zero=False):
    hardware=variables.get('iacp_golden_hardware')
    if not hardware:return
    cpu=actual.get('cpu',[{}])[0]
    expected_vcpus=variables['instance']['cpu_cores']
    hotplugged=cpu.get('hotplugged')
    hotplugged_matches = hotplugged == expected_vcpus
    if (allow_historical_hotplugged_zero and type(hotplugged) is int
            and hotplugged == 0 and expected_vcpus > 0):
        # Algunos planes históricos del provider guardaron vcpus como 0,
        # aunque cores/sockets y el recurso físico sean correctos. Esta
        # compatibilidad sólo se usa en DELETE histórico, nunca en CREATE.
        hotplugged_matches = True
    if (cpu.get('type') != hardware['cpu_type'] or sorted(cpu.get('flags') or [])!=sorted(hardware['flags'])
            or cpu.get('numa') is not hardware['numa'] or cpu.get('sockets',1)!=hardware['sockets']
            or cpu.get('cores',0)*cpu.get('sockets',1)!=expected_vcpus
            # bpg/proxmox 0.116.1 llama ``hotplugged`` al atributo Proxmox
            # ``vcpus``. Debe estar presente en el plan sellado y no heredarse.
            or not hotplugged_matches):
        raise PlanError('GOLDEN_HARDWARE_PLAN_MISMATCH')


def verify_base_plan(actual, variables):
    """Verifica atributos de VM heredados antes de permitir ``apply``."""
    hardware = variables.get('iacp_golden_hardware')
    expected = (hardware or {}).get('terraform_base')
    if not expected:
        return
    try:
        for key in ('bios', 'machine', 'scsi_hardware', 'hotplug', 'boot_order'):
            if actual.get(key) != expected.get(key):
                raise ValueError()
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PlanError('GOLDEN_HARDWARE_PLAN_MISMATCH') from None


def _verified_implicit_disk_default(key, observed, expected):
    """Acepta solo defaults cuya equivalencia está demostrada para Ceph/RBD.

    Proxmox omite estos valores en ``qm config`` cuando representan el valor
    efectivo por defecto. No se ignoran diferencias: cada atributo tiene una
    equivalencia explícita y exige que la Golden Image haya sido observada en
    un backend RBD. ``discard`` e ``iothread`` nunca pasan por esta ruta.
    """
    if expected.get('storage_type') != 'rbd' or not isinstance(expected.get('storage_krbd'), bool):
        return False
    equivalent = {
        # Sin cache explícita, Proxmox genera cache=none para el camino directo
        # de almacenamiento y QEMU usa io_uring cuando el backend lo permite.
        'aio': 'io_uring',
        'cache': 'none',
        # Los valores omitidos por qm.conf son los defaults operativos de PVE.
        'backup': True,
        'replicate': True,
        'ssd': False,
        'queues': 0,
    }
    return key in equivalent and observed == equivalent[key]


def verify_disk_plan(actual, variables):
    hardware = variables.get('iacp_golden_hardware')
    if not hardware or not hardware.get('disk'):
        return
    try:
        expected = hardware['disk']
        requested = variables['instance']['boot_disk']
        if (actual['interface'] != expected['interface']
                or actual['size'] != requested['size_gb']
                or actual['datastore_id'] != requested['datastore_id']):
            raise ValueError()
        for key in _DISK_TECHNICAL_FIELDS:
            observed = actual.get(key)
            source = expected.get(key)
            if observed == source:
                continue
            if source is None and _verified_implicit_disk_default(key, observed, expected):
                continue
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise PlanError('GOLDEN_DISK_PLAN_MISMATCH') from None


def verify_live(config, hardware, expected):
    try:
        source_cpu = hardware.get('source_cpu')
        observed_cpu = config.get('cpu')
        # Proxmox/bpg 0.116.1 serializa el modelo explícito de CPU con el
        # prefijo ``cputype=`` al leer la configuración efectiva. Es una
        # normalización de representación, no un cambio de modelo: la
        # equivalencia solo se admite cuando el valor restante coincide
        # byte por byte con la Golden Image observada.
        if observed_cpu not in {source_cpu, f'cputype={source_cpu}'}:
            raise ValueError()
        normalized_cpu = observed_cpu.removeprefix('cputype=')
        parts=normalized_cpu.split(',')
        cpu_type=parts[0]
        options=dict(part.split('=',1) for part in parts[1:] if '=' in part)
        flags=options.get('flags','').split(';') if options.get('flags') else []
        if (cpu_type!=hardware['cpu_type'] or sorted(flags)!=sorted(hardware['flags'])
                or int(config.get('numa',0)) not in (0,1) or bool(int(config.get('numa',0)))!=hardware['numa']
                or int(config.get('sockets',1))!=hardware['sockets']
                or int(config.get('cores',1))*hardware['sockets']!=expected['cpu_cores']
                or int(config.get('vcpus'))!=expected['cpu_cores']):
            raise ValueError()
        for name,value in hardware['base'].items():
            actual=config.get(name)
            if name=='hotplug' and actual is not None and value is not None:
                if set(actual.split(','))!=set(value.split(',')):raise ValueError()
            elif actual!=value:raise ValueError()
        if hardware.get('disk'):
            disk = hardware['disk']
            actual_disk = _disk(config.get(disk['interface']), disk['interface'], allow_default_queues=True)
            requested = expected['boot_disk']
            if (actual_disk.source_datastore_id != requested['datastore_id']
                    or actual_disk.minimum_size_gb != requested['size_gb']):
                raise ValueError()
            for key in _DISK_TECHNICAL_FIELDS:
                observed = getattr(actual_disk, key)
                source = disk.get(key)
                if observed == source:
                    continue
                if source is None and _verified_implicit_disk_default(key, observed, disk):
                    continue
                raise ValueError()
    except (KeyError,ValueError,TypeError):
        raise PlanError('GOLDEN_HARDWARE_POSTCONDITION_MISMATCH') from None
