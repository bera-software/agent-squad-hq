output "api_url" {
  value = google_cloud_run_v2_service.api.uri
}

output "webhook_url_github" {
  value = "${google_cloud_run_v2_service.api.uri}/api/v1/webhooks/github"
}
