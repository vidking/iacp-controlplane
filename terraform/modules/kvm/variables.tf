variable "vm_name" {
  description = "Nombre DNS de la instancia KVM."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.vm_name))
    error_message = "vm_name debe ser un nombre DNS válido en minúsculas."
  }
}

variable "vm_id" {
  description = "VMID reservado para la instancia de destino."
  type        = number
  nullable    = false

  validation {
    condition = (
      var.vm_id >= 100 &&
      var.vm_id <= 999999999 &&
      floor(var.vm_id) == var.vm_id &&
      var.vm_id != var.template_vm_id
    )
    error_message = "vm_id debe ser un entero válido, distinto del template_vm_id."
  }
}

variable "node_name" {
  description = "Nodo Proxmox donde se alojará la instancia."
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.node_name) != ""
    error_message = "node_name no puede estar vacío."
  }
}

variable "template_vm_id" {
  description = "VMID de la Golden Image de origen."
  type        = number
  nullable    = false

  validation {
    condition     = var.template_vm_id >= 100 && floor(var.template_vm_id) == var.template_vm_id
    error_message = "template_vm_id debe ser un entero válido."
  }
}

variable "template_node_name" {
  description = "Nodo de origen del template. Null utiliza el nodo de destino."
  type        = string
  default     = null
}

variable "cpu_cores" {
  description = "Cantidad de núcleos virtuales de la instancia."
  type        = number
  default     = 2
  nullable    = false

  validation {
    condition     = var.cpu_cores >= 1 && var.cpu_cores <= 128 && floor(var.cpu_cores) == var.cpu_cores
    error_message = "cpu_cores debe ser un entero entre 1 y 128."
  }
}

variable "cpu_type" {
  description = "Tipo de CPU heredado de la Golden Image."
  type        = string
  default     = null
}

variable "cpu_flags" {
  description = "Flags de CPU heredados de la Golden Image."
  type        = set(string)
  default     = []
}

variable "cpu_sockets" {
  description = "Cantidad de sockets virtuales."
  type        = number
  default     = 1

  validation {
    condition     = var.cpu_sockets >= 1 && floor(var.cpu_sockets) == var.cpu_sockets
    error_message = "cpu_sockets debe ser un entero positivo."
  }
}

variable "numa" {
  description = "Habilitar NUMA según la Golden Image."
  type        = bool
  default     = false
}

variable "cpu_hotplugged" {
  description = "Cantidad efectiva de vCPU expuesta por Proxmox."
  type        = number
  default     = null
}

variable "memory_mb" {
  description = "Memoria RAM dedicada en MiB."
  type        = number
  default     = 2048
  nullable    = false

  validation {
    condition     = var.memory_mb >= 512 && var.memory_mb % 256 == 0
    error_message = "memory_mb debe ser >= 512 y múltiplo de 256."
  }
}

variable "network_bridge" {
  description = "Bridge Proxmox para la interfaz principal."
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.network_bridge) != ""
    error_message = "network_bridge no puede estar vacío."
  }
}

variable "vlan_id" {
  description = "VLAN de la interfaz principal."
  type        = number
  default     = null

  validation {
    condition     = var.vlan_id == null || (var.vlan_id >= 1 && var.vlan_id <= 4094 && floor(var.vlan_id) == var.vlan_id)
    error_message = "vlan_id debe estar vacío o entre 1 y 4094."
  }
}

variable "network_mode" {
  description = "Modo de direccionamiento de la interfaz."
  type        = string
  default     = "dhcp"

  validation {
    condition     = contains(["dhcp", "static"], var.network_mode)
    error_message = "network_mode debe ser dhcp o static."
  }
}

variable "ipv4_cidr" {
  description = "Dirección IPv4 estática en formato CIDR."
  type        = string
  default     = null

  validation {
    condition     = var.ipv4_cidr == null || can(cidrnetmask(var.ipv4_cidr))
    error_message = "ipv4_cidr debe ser null o contener una dirección IPv4 y prefijo válidos."
  }
}

variable "ipv4_gateway" {
  description = "Gateway IPv4 de la interfaz principal."
  type        = string
  default     = null

  validation {
    condition     = var.ipv4_gateway == null || can(cidrnetmask("${var.ipv4_gateway}/32"))
    error_message = "ipv4_gateway debe ser null o una dirección IPv4 válida."
  }
}

variable "bios" {
  description = "Firmware heredado de la Golden Image."
  type        = string
  default     = null
}

variable "machine" {
  description = "Tipo de máquina heredado de la Golden Image."
  type        = string
  default     = null
}

variable "scsi_hardware" {
  description = "Controlador SCSI heredado de la Golden Image."
  type        = string
  default     = null
}

variable "hotplug" {
  description = "Dispositivos hotplug heredados de la Golden Image."
  type        = string
  default     = null
}

variable "boot_order" {
  description = "Orden de arranque heredado de la Golden Image."
  type        = list(string)
  default     = null
}

variable "cloudinit_datastore_id" {
  description = "Storage de Proxmox para el disco cloud-init."
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.cloudinit_datastore_id) != ""
    error_message = "cloudinit_datastore_id no puede estar vacío."
  }
}

variable "cloudinit_username" {
  description = "Usuario inicial configurado mediante cloud-init."
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.cloudinit_username) != ""
    error_message = "cloudinit_username no puede estar vacío."
  }
}

variable "ssh_public_keys" {
  description = "Claves SSH públicas autorizadas dentro de la VM."
  type        = list(string)
  nullable    = false

  validation {
    condition = (
      length(var.ssh_public_keys) > 0 &&
      alltrue([for key in var.ssh_public_keys : trimspace(key) != ""])
    )
    error_message = "Debe proporcionarse al menos una clave SSH pública."
  }
}

variable "guest_agent_enabled" {
  description = "Habilitar QEMU Guest Agent cuando la imagen lo soporte."
  type        = bool
  default     = false
}

variable "started" {
  description = "Estado de encendido deseado."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Etiquetas de trazabilidad de la instancia."
  type        = set(string)
  default     = ["iacp"]

  validation {
    condition     = alltrue([for tag in var.tags : can(regex("^[a-z0-9][a-z0-9_-]*$", tag))])
    error_message = "Las etiquetas deben utilizar minúsculas, números, guiones o guiones bajos."
  }
}

variable "pool_id" {
  description = "Pool Proxmox de la instancia."
  type        = string
  default     = null
}

variable "clone_datastore_id" {
  description = "Datastore de destino del clon."
  type        = string
  default     = null
}

variable "boot_disk" {
  description = "Disco de arranque existente en el template. Null conserva el comportamiento heredado. minimum_size_gb debe reflejar el tamaño actual verificado."
  type = object({
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
  })
  default = null

  validation {
    condition = var.boot_disk == null ? true : (
      var.boot_disk.size_gb >= var.boot_disk.minimum_size_gb &&
      var.boot_disk.minimum_size_gb >= 1 &&
      floor(var.boot_disk.size_gb) == var.boot_disk.size_gb &&
      floor(var.boot_disk.minimum_size_gb) == var.boot_disk.minimum_size_gb &&
      can(regex("^(scsi([0-9]|[12][0-9]|30)|sata[0-5]|virtio([0-9]|1[0-5]))$", var.boot_disk.interface)) &&
      trimspace(var.boot_disk.datastore_id) != "" &&
      (var.boot_disk.file_format == null ? true : contains(["raw", "qcow2", "vmdk"], var.boot_disk.file_format)) &&
      contains(["io_uring", "native", "threads"], var.boot_disk.aio) &&
      contains(["none", "writethrough", "writeback", "directsync", "unsafe"], var.boot_disk.cache) &&
      contains(["on", "ignore"], var.boot_disk.discard)
    )
    error_message = "boot_disk requiere interfaz y datastore válidos, tamaños enteros positivos sin reducción y opciones de disco compatibles."
  }
}
