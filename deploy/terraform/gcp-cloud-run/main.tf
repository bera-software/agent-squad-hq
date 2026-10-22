# Agent Squad HQ on Google Cloud Run — EXAMPLE, review before use.
#
# * api        -> Cloud Run service (public HTTPS, scales to N)
# * worker     -> Cloud Run job `worker --once`, run every minute by Cloud Scheduler
# * scheduler  -> Cloud Run job `scheduler --once`, run every minute by Cloud Scheduler
#   (leader election makes overlapping executions safe; idempotency keys dedupe)
# * migrate    -> Cloud Run job, run once per upgrade
# Brings your own: Cloud SQL for Postgres (or any reachable Postgres) and
# Secret Manager secrets.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project
  region  = var.region
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
  jobs = {
    worker    = ["worker", "--once", "--max-jobs", "200"]
    scheduler = ["scheduler", "--once"]
    migrate   = ["migrate"]
  }
}

resource "google_service_account" "runtime" {
  account_id   = "squadhq-runtime"
  display_name = "Agent Squad HQ runtime"
}

resource "google_secret_manager_secret_iam_member" "access" {
  for_each  = var.secrets
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name     = "${local.name}-api"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 1
      max_instance_count = 5
    }
    vpc_access {
      connector = var.vpc_connector
      egress    = "PRIVATE_RANGES_ONLY"
    }
    containers {
      image = var.image
      args  = ["serve"]
      ports {
        container_port = 8080
      }
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      dynamic "env" {
        for_each = local.env
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = var.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value
              version = "latest"
            }
          }
        }
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
      }
      liveness_probe {
        http_get {
          path = "/healthz"
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.access]
}

# Webhooks and API clients authenticate with signatures / bearer tokens,
# so the Cloud Run service itself is public.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.api.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_job" "this" {
  for_each = local.jobs
  name     = "${local.name}-${each.key}"
  location = var.region

  template {
    task_count = 1
    template {
      service_account = google_service_account.runtime.email
      timeout         = each.key == "worker" ? "3300s" : "300s"
      max_retries     = 0
      vpc_access {
        connector = var.vpc_connector
        egress    = "PRIVATE_RANGES_ONLY"
      }
      containers {
        image = var.image
        args  = each.value
        resources {
          limits = { cpu = "1", memory = each.key == "worker" ? "1Gi" : "512Mi" }
        }
        dynamic "env" {
          for_each = local.env
          content {
            name  = env.key
            value = env.value
          }
        }
        dynamic "env" {
          for_each = var.secrets
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = env.value
                version = "latest"
              }
            }
          }
        }
      }
    }
  }
  depends_on = [google_secret_manager_secret_iam_member.access]
}

resource "google_service_account" "invoker" {
  account_id   = "squadhq-scheduler"
  display_name = "Agent Squad HQ Cloud Scheduler invoker"
}

resource "google_cloud_run_v2_job_iam_member" "invoker" {
  for_each = toset(["worker", "scheduler"])
  name     = google_cloud_run_v2_job.this[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.invoker.email}"
}

resource "google_cloud_scheduler_job" "tick" {
  for_each  = toset(["worker", "scheduler"])
  name      = "${local.name}-${each.key}-tick"
  schedule  = "* * * * *"
  time_zone = "Etc/UTC" # the app applies org.timezone itself
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project}/locations/${var.region}/jobs/${google_cloud_run_v2_job.this[each.key].name}:run"
    oauth_token {
      service_account_email = google_service_account.invoker.email
    }
  }
}
