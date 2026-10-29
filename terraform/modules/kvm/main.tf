resource "proxmox_virtual_environment_vm" "this" {
  name      = var.vm_name
  node_name = var.node_name
  vm_id     = var.vm_id
  pool_id   = var.pool_id

  description   = "IACP-managed KVM instance"
  tags          = sort(tolist(var.tags))
  bios          = var.bios
  machine       = var.machine
  scsi_hardware = var.scsi_hardware
  hotplug       = var.hotplug
  boot_order    = var.boot_order

  clone {
    vm_id        = var.template_vm_id
    node_name    = var.template_node_name
    full         = true
    datastore_id = var.clone_datastore_id
  }

  cpu {
    cores      = var.cpu_cores / var.cpu_sockets
    sockets    = var.cpu_sockets
    hotplugged = coalesce(var.cpu_hotplugged, var.cpu_cores)
    type       = var.cpu_type
    flags      = var.cpu_flags
    numa       = var.numa
  }

  memory {
    dedicated = var.memory_mb
    floating  = 0
  }

  # Optativo: sin configuración se conservan los discos heredados del template.
  dynamic "disk" {
    for_each = var.boot_disk == null ? [] : [var.boot_disk]
    content {
      interface    = disk.value.interface
      datastore_id = disk.value.datastore_id
      size         = disk.value.size_gb
      file_format  = disk.value.file_format
      aio          = disk.value.aio
      cache        = disk.value.cache
      discard      = disk.value.discard
      backup       = disk.value.backup
      replicate    = disk.value.replicate
      iothread     = disk.value.iothread
      ssd          = disk.value.ssd
    }
  }

  network_device {
    bridge  = var.network_bridge
    model   = "virtio"
    vlan_id = var.vlan_id
  }

  initialization {
    datastore_id = var.cloudinit_datastore_id

    ip_config {
      ipv4 {
        address = var.network_mode == "dhcp" ? "dhcp" : var.ipv4_cidr
        gateway = var.network_mode == "dhcp" ? null : var.ipv4_gateway
      }
    }

    user_account {
      username = var.cloudinit_username
      keys     = var.ssh_public_keys
    }
  }

  agent {
    enabled = var.guest_agent_enabled

    wait_for_ip {
      disabled = true
    }
  }

  started             = var.started
  on_boot             = false
  reboot_after_update = false

  stop_on_destroy                      = false
  delete_unreferenced_disks_on_destroy = false
}
