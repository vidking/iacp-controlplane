terraform {
  required_version = ">= 1.16.6, < 1.17.0"
}

resource "terraform_data" "instance" {
  input = var.instance
}

output "instance" {
  value = terraform_data.instance.output
}
