variable "environment" {
  description = "Identificador del entorno de pruebas."
  type        = string
  nullable    = false

  validation {
    condition     = var.environment == "mng01"
    error_message = "Este proyecto solo ejecuta sobre el entorno mng01."
  }
}

variable "allowed_nodes" {
  description = "Nodo Proxmox de pruebas permitido."
  type        = set(string)
  nullable    = false

  validation {
    condition = (
      length(var.allowed_nodes) > 0 &&
      alltrue([
        for node in var.allowed_nodes : trimspace(node) != ""
      ])
    )
    error_message = "allowed_nodes debe contener mng01."
  }
}

variable "instance" {
  description = "Contrato de aprovisionamiento de una instancia KVM."
  nullable    = false

  type = object({
    vm_name            = string
    vm_id              = number
    pool_id            = optional(string)
    clone_datastore_id = optional(string)
    boot_disk = optional(object({
      interface       = string
      datastore_id    = string
      size_gb         = number
      minimum_size_gb = number
      file_format     = optional(string)
      aio             = optional(string, "io_uring")
      cache           = optional(string, "none")
      discard         = optional(string, "ignore")
      backup          = optional(bool, true)
      replicate       = optional(bool, true)
      iothread        = optional(bool, false)
      ssd             = optional(bool, false)
    }))
    node_name              = string
    template_vm_id         = number
    template_node_name     = optional(string)
    cpu_cores              = optional(number, 2)
    cpu_type               = optional(string, null)
    cpu_flags              = optional(set(string), [])
    cpu_sockets            = optional(number, 1)
    numa                   = optional(bool, false)
    cpu_hotplugged         = optional(number, null)
    memory_mb              = optional(number, 2048)
    network_bridge         = string
    network_mode           = optional(string, "dhcp")
    vlan_id                = optional(number, null)
    ipv4_cidr              = optional(string, null)
    ipv4_gateway           = optional(string, null)
    cloudinit_datastore_id = string
    cloudinit_username     = string
    ssh_public_keys        = list(string)
    guest_agent_enabled    = optional(bool, false)
    started                = optional(bool, false)
    bios                   = optional(string, null)
    machine                = optional(string, null)
    scsi_hardware          = optional(string, null)
    hotplug                = optional(string, null)
    boot_order             = optional(list(string), null)
    tags                   = optional(set(string), ["iacp"])
  })

  validation {
    condition = contains(
      var.allowed_nodes,
      var.instance.node_name
    )
    error_message = "El nodo destino no pertenece a allowed_nodes."
  }

  validation {
    condition     = contains(["dhcp", "static"], var.instance.network_mode)
    error_message = "network_mode debe ser dhcp o static."
  }

  validation {
    condition = var.instance.network_mode == "dhcp" ? (
      var.instance.ipv4_cidr == null && var.instance.ipv4_gateway == null
      ) : (
      var.instance.ipv4_cidr != null && var.instance.ipv4_gateway != null
    )
    error_message = "DHCP no acepta IPv4/gateway y static requiere ambos valores."
  }
}
