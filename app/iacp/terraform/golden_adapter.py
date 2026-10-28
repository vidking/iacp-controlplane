"""Adaptación declarativa sellada: cantidades del plan, hardware de la Golden Image.

Reutiliza el mecanismo de adaptadores del stack fijado. No modifica el repositorio
IaC ni las plantillas y no altera workspaces históricos sin esta capacidad.
"""
import hashlib
import json
from .config import PlanError

FILES={
 'modules/compute/kvm/iacp_golden_override.tf.json':json.dumps({'resource':{'proxmox_virtual_environment_vm':{'this':{'cpu':[{
  'cores':'${var.cpu_cores / var.golden_hardware.sockets}',
  'sockets':'${var.golden_hardware.sockets}',
  # bpg/proxmox 0.116.1 serializa el número efectivo de vCPU de Proxmox
  # (campo ``vcpus``) como ``hotplugged`` en este recurso. El plan debe
  # sobrescribir explícitamente el valor heredado de la plantilla.
  'hotplugged':'${var.cpu_cores}',
  'type':'${var.golden_hardware.cpu_type}',
  'flags':'${var.golden_hardware.flags}',
  'numa':'${var.golden_hardware.numa}'
 }],
  'bios':'${try(var.golden_hardware.terraform_base.bios, null)}',
  'machine':'${try(var.golden_hardware.terraform_base.machine, null)}',
  'scsi_hardware':'${try(var.golden_hardware.terraform_base.scsi_hardware, null)}',
  'hotplug':'${try(var.golden_hardware.terraform_base.hotplug, null)}',
  'boot_order':'${try(var.golden_hardware.terraform_base.boot_order, null)}'
 }}}},sort_keys=True),
}
MAIN_ARGUMENT='  golden_hardware = var.iacp_golden_hardware\n'
DIGEST=hashlib.sha256(json.dumps({'files':FILES,'main_argument':MAIN_ARGUMENT},sort_keys=True).encode()).hexdigest()

# Una KVM histórica conserva el adaptador que selló su plan de creación. La
# compatibilidad aquí es deliberadamente explícita: solo se admite la revisión
# IaC conocida que produjo el artefacto observado y se vuelven a comprobar los
# archivos exactos antes de planificar el destroy.
LEGACY_ADAPTERS={
 '1ea5fcaeb868d7a7b3423cde2415e827aca497cfbfd7f1835b3a3f1b5358415e': {
  'source_revision': '3a2fbecdb89f580e58502bd4ef9f968b167ca260',
  'main_argument': MAIN_ARGUMENT,
  'files': {
   'modules/compute/kvm/iacp_golden_override.tf.json': json.dumps({'resource':{'proxmox_virtual_environment_vm':{'this':{'bios':'${try(var.golden_hardware.terraform_base.bios, null)}','boot_order':'${try(var.golden_hardware.terraform_base.boot_order, null)}','cpu':[{'cores':'${var.cpu_cores / var.golden_hardware.sockets}','flags':'${var.golden_hardware.flags}','numa':'${var.golden_hardware.numa}','sockets':'${var.golden_hardware.sockets}','type':'${var.golden_hardware.cpu_type}'}],'hotplug':'${try(var.golden_hardware.terraform_base.hotplug, null)}','machine':'${try(var.golden_hardware.terraform_base.machine, null)}','scsi_hardware':'${try(var.golden_hardware.terraform_base.scsi_hardware, null)}'}}}},sort_keys=True),
  },
 },
}


def _adapter_spec(manifest):
    digest=manifest.get('golden_adapter_sha256')
    if digest == DIGEST:
        return FILES, MAIN_ARGUMENT
    legacy=LEGACY_ADAPTERS.get(digest)
    if legacy is None or manifest.get('source_revision') != legacy['source_revision']:
        raise PlanError('PLAN_INTEGRITY_FAILED')
    return legacy['files'], legacy['main_argument']


def install(source):
    from .access_adapter import BASE_MAIN, FILES as ACCESS_FILES
    main=source/'stacks/kvm-instance/main.tf'
    original=main.read_text()
    if original not in (BASE_MAIN,ACCESS_FILES['stacks/kvm-instance/main.tf']):
        raise PlanError('GOLDEN_ADAPTER_SOURCE_MISMATCH')
    main.write_text(original.replace('  source =',MAIN_ARGUMENT+'  source =',1))
    main.chmod(0o600)
    for name,value in FILES.items():
        target=source/name
        if target.exists():raise PlanError('GOLDEN_ADAPTER_ALREADY_EXISTS')
        target.write_text(value+'\n');target.chmod(0o600)


def verify(source,manifest,variables):
    required='iacp_golden_hardware' in variables
    if required:
        files,main_argument=_adapter_spec(manifest)
        try:
            if any((source/name).read_text()!=value+'\n' for name,value in files.items()):raise ValueError()
            from .access_adapter import BASE_MAIN, FILES as ACCESS_FILES
            original=ACCESS_FILES['stacks/kvm-instance/main.tf'] if 'iacp_root_password' in variables else BASE_MAIN
            if (source/'stacks/kvm-instance/main.tf').read_text()!=original.replace('  source =',main_argument+'  source =',1):raise ValueError()
        except (OSError,ValueError):raise PlanError('PLAN_INTEGRITY_FAILED') from None
    elif manifest.get('golden_adapter_sha256') or any((source/name).exists() for name in FILES):
        raise PlanError('PLAN_INTEGRITY_FAILED')
