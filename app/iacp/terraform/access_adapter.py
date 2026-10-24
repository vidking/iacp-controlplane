"""Reviewed additive adapter for the pinned stack; never edits the Golden Image.

Only password-bearing lab intents install these files. Historical workspaces and
source revision remain intact. The exact adapter digest is sealed with the plan.
"""
import hashlib
import json

from .config import PlanError

VARIABLE = '''variable "iacp_root_password" {
  type      = string
  sensitive = true
  nullable  = false
}
'''
BASE_MAIN = '''module "kvm" {
  source = "../../modules/compute/kvm"

  providers = {
    proxmox = proxmox
  }

  vm_name                = var.instance.vm_name
  vm_id                  = var.instance.vm_id
  pool_id                = var.instance.pool_id
  clone_datastore_id     = var.instance.clone_datastore_id
  boot_disk              = var.instance.boot_disk
  node_name              = var.instance.node_name
  template_vm_id         = var.instance.template_vm_id
  template_node_name     = var.instance.template_node_name
  cpu_cores              = var.instance.cpu_cores
  memory_mb              = var.instance.memory_mb
  network_bridge         = var.instance.network_bridge
  network_mode           = var.instance.network_mode
  vlan_id                = var.instance.vlan_id
  ipv4_cidr              = var.instance.ipv4_cidr
  ipv4_gateway           = var.instance.ipv4_gateway
  cloudinit_datastore_id = var.instance.cloudinit_datastore_id
  cloudinit_username     = var.instance.cloudinit_username
  ssh_public_keys        = var.instance.ssh_public_keys
  guest_agent_enabled    = var.instance.guest_agent_enabled
  started                = var.instance.started

  tags = setunion(
    var.instance.tags,
    toset(["iacp", var.environment])
  )
}
'''
FILES = {
    'stacks/kvm-instance/iacp_access_variables.tf': VARIABLE,
    'modules/compute/kvm/iacp_access_variables.tf': VARIABLE,
    'stacks/kvm-instance/main.tf': BASE_MAIN.replace('  source =', '  iacp_root_password = var.iacp_root_password\n  source =', 1),
    'modules/compute/kvm/iacp_access_override.tf': '''
resource "proxmox_virtual_environment_vm" "this" {
  initialization {
    datastore_id = var.cloudinit_datastore_id
    upgrade = false
    ip_config {
      ipv4 {
        address = var.network_mode == "dhcp" ? "dhcp" : var.ipv4_cidr
        gateway = var.network_mode == "dhcp" ? null : var.ipv4_gateway
      }
    }
    user_account {
      username = var.cloudinit_username
      keys     = var.ssh_public_keys
      password = var.iacp_root_password
    }
  }
}
''',
}
DIGEST = hashlib.sha256(json.dumps(FILES, sort_keys=True).encode()).hexdigest()


def install(source):
    for name, content in FILES.items():
        path = source / name
        if name == 'stacks/kvm-instance/main.tf':
            if not path.is_file() or path.read_text() != BASE_MAIN:
                raise PlanError('ACCESS_ADAPTER_SOURCE_MISMATCH')
        elif path.exists():
            raise PlanError('ACCESS_ADAPTER_ALREADY_EXISTS')
        path.write_text(content)
        path.chmod(0o600)


def verify(source, manifest, variables):
    if not isinstance(manifest, dict):
        raise PlanError('PLAN_INTEGRITY_FAILED')
    required = 'iacp_root_password' in variables
    if required:
        if manifest.get('access_adapter_sha256') != DIGEST:
            raise PlanError('PLAN_INTEGRITY_FAILED')
        try:
            if any((source / name).read_text() != content for name, content in FILES.items()):
                raise ValueError()
        except (OSError, ValueError):
            raise PlanError('PLAN_INTEGRITY_FAILED') from None
    elif manifest.get('access_adapter_sha256') or any((source / name).exists() for name in FILES if name != 'stacks/kvm-instance/main.tf'):
        raise PlanError('PLAN_INTEGRITY_FAILED')
