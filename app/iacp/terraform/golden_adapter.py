"""Adaptación declarativa sellada: cantidades del plan, hardware de la Golden Image.

Reutiliza el mecanismo de adaptadores del stack fijado. No modifica el repositorio
IaC ni las plantillas y no altera workspaces históricos sin esta capacidad.
"""
import hashlib
import json
from .config import PlanError

FILES={
 'stacks/kvm-instance/iacp_golden_variables.tf.json':json.dumps({'variable':{'iacp_golden_hardware':{'type':'any','nullable':False}}},sort_keys=True),
 'modules/compute/kvm/iacp_golden_variables.tf.json':json.dumps({'variable':{'golden_hardware':{'type':'any','nullable':False}}},sort_keys=True),
 'modules/compute/kvm/iacp_golden_override.tf.json':json.dumps({'resource':{'proxmox_virtual_environment_vm':{'this':{'cpu':[{
  'cores':'${var.cpu_cores / var.golden_hardware.sockets}',
  'sockets':'${var.golden_hardware.sockets}',
  'flags':'${var.golden_hardware.flags}',
  'numa':'${var.golden_hardware.numa}'
 }]}}}},sort_keys=True),
}
MAIN_ARGUMENT='  golden_hardware = var.iacp_golden_hardware\n'
DIGEST=hashlib.sha256(json.dumps({'files':FILES,'main_argument':MAIN_ARGUMENT},sort_keys=True).encode()).hexdigest()


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
        if manifest.get('golden_adapter_sha256')!=DIGEST:raise PlanError('PLAN_INTEGRITY_FAILED')
        try:
            if any((source/name).read_text()!=value+'\n' for name,value in FILES.items()):raise ValueError()
            from .access_adapter import BASE_MAIN, FILES as ACCESS_FILES
            original=ACCESS_FILES['stacks/kvm-instance/main.tf'] if 'iacp_root_password' in variables else BASE_MAIN
            if (source/'stacks/kvm-instance/main.tf').read_text()!=original.replace('  source =',MAIN_ARGUMENT+'  source =',1):raise ValueError()
        except (OSError,ValueError):raise PlanError('PLAN_INTEGRITY_FAILED') from None
    elif manifest.get('golden_adapter_sha256') or any((source/name).exists() for name in FILES):
        raise PlanError('PLAN_INTEGRITY_FAILED')
