output "api_url" {
  value = "https://${azurerm_container_app.this["api"].ingress[0].fqdn}"
}

output "webhook_url_github" {
  value = "https://${azurerm_container_app.this["api"].ingress[0].fqdn}/api/v1/webhooks/github"
}
