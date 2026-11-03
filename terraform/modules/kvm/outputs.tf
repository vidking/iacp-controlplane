output "vm_id" {
  description = "VMID administrado por Terraform."
  value       = proxmox_virtual_environment_vm.this.vm_id
}

output "vm_name" {
  description = "Nombre de la instancia."
  value       = proxmox_virtual_environment_vm.this.name
}

output "node_name" {
  description = "Nodo asignado a la instancia."
  value       = proxmox_virtual_environment_vm.this.node_name
}

output "configured_ipv4_cidr" {
  description = "Configuración IPv4 declarada, no confirmación de conectividad."
  value       = var.ipv4_cidr
}
output "configured_boot_disk" {
  description = "Configuración solicitada del disco; null conserva la herencia. No acredita un resize aplicado."
  value       = var.boot_disk
}
