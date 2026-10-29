module "kvm" {
  source = "../../modules/kvm"

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
  cpu_type               = var.instance.cpu_type
  cpu_flags              = var.instance.cpu_flags
  cpu_sockets            = var.instance.cpu_sockets
  numa                   = var.instance.numa
  cpu_hotplugged         = var.instance.cpu_hotplugged
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
  bios                   = var.instance.bios
  machine                = var.instance.machine
  scsi_hardware          = var.instance.scsi_hardware
  hotplug                = var.instance.hotplug
  boot_order             = var.instance.boot_order

  tags = setunion(
    var.instance.tags,
    toset(["iacp", var.environment])
  )
}
