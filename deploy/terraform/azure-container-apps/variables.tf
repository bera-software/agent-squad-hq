variable "location" {
  type    = string
  default = "uksouth"
}

variable "image" {
  description = "Image reference, pinned by digest"
  type        = string
}

variable "hub_repo" {
  type = string
}

variable "subnet_id" {
  description = "Subnet for the Container Apps environment (must reach PostgreSQL)"
  type        = string
}

variable "key_vault_id" {
  type = string
}

variable "key_vault_secret_ids" {
  description = "Env var name -> Key Vault secret id (versionless URI). SQUADHQ_DB_URL is required."
  type        = map(string)
  validation {
    condition     = contains(keys(var.key_vault_secret_ids), "SQUADHQ_DB_URL")
    error_message = "key_vault_secret_ids must include SQUADHQ_DB_URL."
  }
}

variable "squadhq_yml" {
  type = string
}

variable "extra_env" {
  type    = map(string)
  default = {}
}
