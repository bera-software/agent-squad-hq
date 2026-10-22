output "api_url" {
  value = "https://${aws_lb.this.dns_name}"
}

output "webhook_url_github" {
  value = "https://${aws_lb.this.dns_name}/api/v1/webhooks/github"
}

output "migrate_task_definition" {
  description = "Run once per upgrade: aws ecs run-task --task-definition <this> ..."
  value       = aws_ecs_task_definition.migrate.arn
}
