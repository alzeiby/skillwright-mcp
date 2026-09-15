output "ecr_repository_url" {
  value = aws_ecr_repository.skillwright.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.skillwright.name
}

output "alb_dns_name" {
  value = aws_lb.skillwright.dns_name
}

output "api_url" {
  value = var.external_base_url
}

output "mcp_url" {
  value = "${var.external_base_url}/mcp"
}

output "database_endpoint" {
  value = aws_db_instance.postgres.endpoint
}

output "rds_master_secret_arn" {
  description = "AWS-managed secret containing the RDS master credentials; the secret value is not exposed."
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "runtime_database_secret_arn" {
  description = "Secrets Manager ARN initialized by the migration task with least-privilege runtime PostgreSQL credentials."
  value       = aws_secretsmanager_secret.database_runtime.arn
}

output "migration_marker_parameter" {
  description = "SSM parameter recording the image tag whose database migration most recently completed."
  value       = aws_ssm_parameter.migration_marker.name
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.redis.primary_endpoint_address
}

output "migration_task_definition_arn" {
  value = aws_ecs_task_definition.migration.arn
}

output "ecs_task_security_group_id" {
  value = aws_security_group.tasks.id
}

output "private_subnet_ids" {
  value = var.private_subnet_ids
}
