variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "image" {
  description = "Image reference, ideally pinned by digest (…/agent-squad-hq@sha256:…)"
  type        = string
}

variable "hub_repo" {
  description = "owner/name of the hub repository"
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "certificate_arn" {
  description = "ACM certificate for the ALB HTTPS listener"
  type        = string
}

variable "allowed_ingress_cidrs" {
  description = "Who may reach the API. GitHub webhook ranges: https://api.github.com/meta (hooks)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "secret_arns" {
  description = "Env var name -> plain Secrets Manager secret ARN (one secret per variable). SQUADHQ_DB_URL is required."
  type        = map(string)
  validation {
    condition     = contains(keys(var.secret_arns), "SQUADHQ_DB_URL")
    error_message = "secret_arns must include SQUADHQ_DB_URL."
  }
}

variable "squadhq_yml" {
  description = "Contents of squadhq.yml"
  type        = string
}

variable "extra_env" {
  description = "Additional non-secret SQUADHQ_* settings"
  type        = map(string)
  default     = {}
}
