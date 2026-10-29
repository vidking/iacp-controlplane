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
    memory_mb              = optional(number, 2048)
    network_bridge         = string
    vlan_id                = number
    ipv4_cidr              = string
    ipv4_gateway           = string
    cloudinit_datastore_id = string
    cloudinit_username     = string
    ssh_public_keys        = list(string)
    guest_agent_enabled    = optional(bool, false)
    started                = optional(bool, false)
    tags                   = optional(set(string), ["iacp"])
  })

  validation {
    condition = contains(
      var.allowed_nodes,
      var.instance.node_name
    )
    error_message = "El nodo destino no pertenece a allowed_nodes."
  }
}
