# Agent Squad HQ on Azure Container Apps — EXAMPLE, review before use.
#
# * api       -> container app with external HTTPS ingress
# * worker    -> container app without ingress (long-running loop)
# * scheduler -> container app without ingress (leader-elected loop)
# * migrate   -> Container Apps Job (manual trigger; run once per upgrade)
# Brings your own: Azure Database for PostgreSQL and a Key Vault holding the
# secrets; a user-assigned identity reads them.

terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

locals {
  name = "agent-squad-hq"
  env = merge({
    SQUADHQ_HUB_REPO        = var.hub_repo
    SQUADHQ_MODE            = "byo-compute"
    SQUADHQ_LOG_FORMAT      = "json"
    SQUADHQ_CONFIG_YAML     = var.squadhq_yml
    SQUADHQ_TRUSTED_PROXIES = "*"
  }, var.extra_env)
  # Container Apps secret names must be lowercase alphanumerics and dashes
  secret_names = { for k, _ in var.key_vault_secret_ids : k => lower(replace(k, "_", "-")) }
  apps = {
    api       = { args = ["serve"], cpu = 0.5, memory = "1Gi", min = 1, max = 4 }
    worker    = { args = ["worker"], cpu = 0.5, memory = "1Gi", min = 1, max = 4 }
    scheduler = { args = ["scheduler"], cpu = 0.25, memory = "0.5Gi", min = 1, max = 2 }
  }
}

resource "azurerm_resource_group" "this" {
  name     = "rg-${local.name}"
  location = var.location
}

resource "azurerm_log_analytics_workspace" "this" {
  name                = "log-${local.name}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_container_app_environment" "this" {
  name                       = "cae-${local.name}"
  location                   = azurerm_resource_group.this.location
  resource_group_name        = azurerm_resource_group.this.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.this.id
  infrastructure_subnet_id   = var.subnet_id
}

resource "azurerm_user_assigned_identity" "this" {
  name                = "id-${local.name}"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
}

resource "azurerm_role_assignment" "kv" {
  scope                = var.key_vault_id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
}

resource "azurerm_container_app" "this" {
  for_each                     = local.apps
  name                         = "${local.name}-${each.key}"
  container_app_environment_id = azurerm_container_app_environment.this.id
  resource_group_name          = azurerm_resource_group.this.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  dynamic "secret" {
    for_each = var.key_vault_secret_ids
    content {
      name                = local.secret_names[secret.key]
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.this.id
    }
  }

  dynamic "ingress" {
    for_each = each.key == "api" ? [1] : []
    content {
      external_enabled = true
      target_port      = 8080
      transport        = "http"
      traffic_weight {
        latest_revision = true
        percentage      = 100
      }
    }
  }

  template {
    min_replicas = each.value.min
    max_replicas = each.value.max
    container {
      name   = each.key
      image  = var.image
      args   = each.value.args
      cpu    = each.value.cpu
      memory = each.value.memory
      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = local.secret_names
        content {
          name        = env.key
          secret_name = env.value
        }
      }
      dynamic "liveness_probe" {
        for_each = each.key == "api" ? [1] : []
        content {
          transport = "HTTP"
          port      = 8080
          path      = "/healthz"
        }
      }
      dynamic "readiness_probe" {
        for_each = each.key == "api" ? [1] : []
        content {
          transport = "HTTP"
          port      = 8080
          path      = "/readyz"
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.kv]
}

resource "azurerm_container_app_job" "migrate" {
  name                         = "${local.name}-migrate"
  location                     = azurerm_resource_group.this.location
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  replica_timeout_in_seconds   = 600
  replica_retry_limit          = 1
  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }
  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }
  dynamic "secret" {
    for_each = var.key_vault_secret_ids
    content {
      name                = local.secret_names[secret.key]
      key_vault_secret_id = secret.value
      identity            = azurerm_user_assigned_identity.this.id
    }
  }
  template {
    container {
      name   = "migrate"
      image  = var.image
      args   = ["migrate"]
      cpu    = 0.25
      memory = "0.5Gi"
      dynamic "env" {
        for_each = local.secret_names
        content {
          name        = env.key
          secret_name = env.value
        }
      }
    }
  }
  depends_on = [azurerm_role_assignment.kv]
}
