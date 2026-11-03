terraform {
  required_version = ">= 1.16.6, < 1.17.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "= 0.116.1"
    }
  }
}
