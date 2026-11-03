output "vm_id" {
  description = "Identificador de la instancia KVM."
  value       = module.kvm.vm_id
}

output "vm_name" {
  description = "Nombre asignado a la instancia."
  value       = module.kvm.vm_name
}

output "node_name" {
  description = "Nodo de destino."
  value       = module.kvm.node_name
}

output "configured_ipv4_cidr" {
  description = "IPv4 configurada, sin garantizar conectividad."
  value       = module.kvm.configured_ipv4_cidr
}

output "environment" {
  description = "Entorno de ejecución."
  value       = var.environment
}
output "configured_boot_disk" {
  description = "Configuración solicitada del disco de arranque."
  value       = module.kvm.configured_boot_disk
}
