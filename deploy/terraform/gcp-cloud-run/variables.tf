variable "project" {
  type = string
}

variable "region" {
  type    = string
  default = "europe-west2"
}

variable "image" {
  description = "Artifact Registry image, pinned by digest"
  type        = string
}

variable "hub_repo" {
  type = string
}

variable "vpc_connector" {
  description = "Serverless VPC Access connector used to reach Cloud SQL / private Postgres"
  type        = string
}

variable "secrets" {
  description = "Env var name -> Secret Manager secret id. SQUADHQ_DB_URL is required."
  type        = map(string)
  validation {
    condition     = contains(keys(var.secrets), "SQUADHQ_DB_URL")
    error_message = "secrets must include SQUADHQ_DB_URL."
  }
}

variable "squadhq_yml" {
  type = string
}

variable "extra_env" {
  type    = map(string)
  default = {}
}
